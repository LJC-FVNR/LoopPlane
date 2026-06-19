# LoopPlane Dashboard

This directory contains the framework-free dashboard assets used by
`loopplane dashboard`.

Use `loopplane dashboard --project <project>` for offline/static generation,
`loopplane dashboard --project <project> --port <port>` for fixed-port server
mode, and `loopplane dashboard --project <project> --port auto` for automatic
local port allocation. Add `--workflow <workflow_id>` to select a workflow from
`.loopplane/workflow_registry.json` for visualization without updating
`.loopplane/current_workflow.json`. Use `loopplane dashboard list --project <project>`
to inspect workspace-scoped `dashboard_server.json` records across registered
workflow histories without starting a server or changing workflow truth. The
static output is a read-only snapshot; server mode is required for
request-entry controls. Without `--port`, the
command reads the selected workflow read-model directory and writes a derived
static bundle, defaulting to `.loopplane/dashboard_static/` in the flat
compatibility layout:

- `index.html` with an embedded read-model snapshot;
- `static_dashboard.css` and `static_dashboard.js`;
- `read_models/` copies of the dashboard read-model files.

The generated view is read-only against authoritative workflow state. It shows
the current workspace, a workflow-history selector, workflow status, the plan
checklist, workflow graph, selected node details, activity feed, read-model
links, sanitized Git checkpoint status from `version_control_status.json`, and
a runner configuration panel backed by `.loopplane/config/agent_runners.json`.
Selected workflow graph nodes read their details from embedded read models.
For run-backed nodes, the detail panel can show bounded prompt, final response,
report, log, validation, artifact, project-change, Git checkpoint, diff
summary, and event evidence from `run_summaries.jsonl`. Optional evidence
renders as an empty state when absent. Paths, log text, artifact previews,
diff metadata, and checkpoint records are sanitized so the bundle does not
expose `.git/` internals, checkpoint refs, repository roots, or common secret
patterns.
It also renders planner, auditor, and plan-activation controls in read-only
mode so offline views show the available operations without writing requests.
Start, pause, resume, and stop controls are rendered the same way in static
mode: visible for orientation, but disabled because the offline bundle cannot
append request records. The approval panel also renders in static mode with
pending and recent approval details, but approve/reject actions are disabled
until the dashboard is opened in server mode.
The bottom inspector console renders full-agent inspector chat and
change-request forms in static mode as disabled affordances, plus recent
chat/change request records when request files are present.
When the workflow read model contains pending approval or other
`requires_attention` items, the page shows an attention band with the relevant
CLI command hints, such as `loopplane approvals`, `loopplane approve`, and
`loopplane reject`.

If `.loopplane/workflow_registry.json` lists multiple workflow histories, static
output embeds read-model snapshots for available workflows so the selector can
switch views without editing `.loopplane/current_workflow.json`. Registry entries
whose read models are missing remain visible and are labeled unavailable.

The renderer compares read-model event metadata with the current event-log
projection before writing `index.html`. If the read models are stale, the
render report includes `read_model_freshness` and a warning, and the generated
page shows a freshness banner with a `loopplane rebuild-read-models --project ...`
`--workflow <workflow_id>` command hint plus a disabled rebuild request form.
Missing freshness metadata is shown as an unknown freshness warning with the
same affordance. Passing `--rebuild-read-models` refreshes derived read models
before rendering; otherwise the dashboard does not mutate authoritative state.

With `--port <port>` or `--port auto`, the same assets are served by a local
HTTP server bound to the configured dashboard host, defaulting to `127.0.0.1`.
Auto mode reads `.loopplane/config/dashboard.json`, tries `preferred_port` first,
and then skips occupied ports within `port_range`. Server mode requires the
dashboard token by default; in the flat compatibility layout the token lives at
`.loopplane/runtime/dashboard_token`, and startup records local process metadata at
`.loopplane/runtime/dashboard_server.json`. Canonical v1.6 workflows store both
files under the selected workflow's resolved `runtime_dir`. The metadata
includes the URL, host, allocated port, PID, start time, workspace ID, current
workflow ID, selected workflow ID, token-file path, and server-state path.
Startup also upserts a token-free machine-local discovery record in
`$LOOPPLANE_HOME/dashboard/servers.json`, honoring the `LOOPPLANE_HOME` environment
override and creating the LOOPPLANE_HOME layout when needed. That discovery index
stores project/workspace IDs, URL, host, port, PID, and the project-local
server-state path, but not token values or token-file paths.
The token file is created or reused with restrictive permissions where the
platform supports them. The server accepts the token through `Authorization:
Bearer ...` or `X-LoopPlane-Token`; same-origin browser GETs may also use the
`?token=...` query parameter. Mutating requests require token auth and the
same-origin check.

Read endpoints expose workspace, workflow, status, plan, graph, run, approval,
runner, dashboard-data, and control snapshots. `GET /api/workspace`,
`GET /api/workspace/workflows`, and
`GET /api/workspace/workflows/<workflow_id>` resolve only project-local
workspace registry entries; workflow detail responses include the selected
workflow read-model dashboard payload and do not update
`.loopplane/current_workflow.json`. Direct file serving is limited to the static
dashboard assets and known read-model filenames; traversal such as reading
runtime state through `/read_models/..` is rejected. Most mutating endpoints
create LoopPlane request records or approval/change-response records and do not
edit `PLAN.md` directly. The explicit `POST /api/workspace/workflows` route is
the workflow-create exception: it delegates to the same safe create path as
`loopplane workflow create`, allocating a new workflow history and updating
registry/current-pointer metadata only after policy checks pass. The server
dashboard enables planner, auditor, and `activate-plan` controls only for
mutable workflow histories; the controls POST to `/api/workflows/<id>/plan`,
`/api/workflows/<id>/audit`, and `/api/workflows/<id>/activate-plan`, which
append records to `dashboard_requests.jsonl` under the resolved workflow
`requests_dir`. It also enables start, pause, resume, and stop controls for
mutable workflow histories;
those controls POST to `/api/workflows/<id>/control-requests` and append
records to the workflow runtime `control_requests.jsonl` file. The browser
path does not start, pause, resume, or stop the runtime directly; the
scheduler or detached supervisor applies control requests at safe points.
Mutating endpoints require token auth plus same-origin checks, dashboard
request/response bodies are redacted for common secret patterns, and archived,
read-only, immutable, or policy-ineligible workflow histories reject mutation.
The server does not expose arbitrary shell execution or direct `.git/`
internals; version-control status comes from sanitized read models and
checkpoint metadata.
When freshness is stale or unknown, server mode enables the freshness banner's
rebuild request form for mutable workflow histories. The form POSTs to
`/api/workflows/<id>/rebuild-read-models`, which appends a
`rebuild_read_models` control request to the workflow runtime
`control_requests.jsonl` file. The browser path does not run
`loopplane rebuild-read-models` directly and does not edit read models,
authoritative runtime state, event logs, completion markers, or Git state.
`GET /api/workflows/<id>/runs/<run_id>` returns the same read-model-backed run
detail sections used by the static detail panel; it does not inspect `.git/`
directories or mutate runtime state.
The server dashboard enables approval responses only for mutable workflow
histories with interactive approval enabled. Approval actions POST to
`/api/workflows/<id>/approvals/<approval_id>/respond`, append
`human_approval_responses.jsonl` records under the resolved runtime directory,
refresh the approval list in-place, and do not directly edit `PLAN.md`,
runtime state, event logs, or read models.
The server dashboard also enables the bottom inspector console for mutable
workflow histories. Inspector chat submits to `/api/workflows/<id>/chat` with
the `inspector` runner, runs that agent, and appends `chat_requests.jsonl`
plus `chat_responses.jsonl`; change requests submit to
`/api/workflows/<id>/change-requests` and append
`change_requests.jsonl` under the resolved workflow `requests_dir`. These
paths use dashboard token and same-origin protections and refresh recent console
records in-place.
logs, validation files, read models, or completion markers.
The browser selector uses same-origin GET requests to
`/api/workflows/<workflow_id>/dashboard-data` to load a selected workflow's
read-model snapshot. This changes only the displayed dashboard context; it does
not switch the active workflow pointer.

`loopplane dashboard list` reads only project-local workspace metadata and each
workflow's configured server-state path, defaulting to
`<runtime_dir>/dashboard_server.json`, and matching machine-local records from
`$LOOPPLANE_HOME/dashboard/servers.json`. Missing records are reported as a
successful empty/no-active-dashboard result. Existing records are summarized
with workflow name, workflow ID, status, created and last-seen timestamps,
progress summary, completion marker freshness, Git checkpoint summary,
archived/read-only labels, server URL/host/port/PID, selected workflow ID,
PID-based liveness/staleness, and stale-record guidance. Tokens and sensitive
fields are redacted.

Runner configuration stays behind the dashboard security boundary. When
`security.json` does not enable dashboard trusted-local mode, runner commands
are hidden and the panel is read-only. When trusted-local mode is enabled,
server mode shows redacted command labels, roles, adapters, enabled state,
prompt delivery modes, timeouts, and doctor diagnostics, and can POST
`runner_configuration` request records. The browser path does not edit
`agent_runners.json` directly and rejects environment values, shell fragments,
and local path commands. Configure exact local runner commands with
`loopplane configure-agent`; the CLI stores them under the resolved `LOOPPLANE_HOME`
runner override file rather than portable workflow truth. These machine-local
runner overrides are not portable and should be recreated after copying or
importing a project.

Server/API/static smoke coverage is enforced by the release validation suite,
including dashboard, security-boundary, read-model, control, workflow-history,
and package-tree tests.
