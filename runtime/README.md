# LoopPlane Runtime

This directory contains scheduler, prompt builder, validator, reconciler, final
verifier, read-model builder, and runtime support code.

## v1.7 Lightweight Runtime Status

Routine control-plane operations are bounded by default. Git checkpoint work
uses scoped probes and explicit budgets, scheduler waits avoid repeated full
snapshot rebuilds, recursive metadata discovery prunes generated artifacts, and
normal health probes inspect recent operational history. Expensive historical
integrity work remains available through explicit operations such as
`health --strict`.

## v1.6 Runtime Support Status

Runtime modules support same-workspace workflow history, dashboard switching by
selected workflow ID, project-local workspace registry/current workflow pointer
truth with v1.5 flat compatibility, canonical workflow-root path resolution,
LOOPPLANE_HOME discovery/local overrides as non-authoritative machine state, runner
resource locks, monorepo and nested workspace boundary enforcement, stale-state
excluding migration export/import profiles, Git-ref bundle export/import, and
archived/read-only workflow mutation safeguards. Release validation exercises
these surfaces through package-tree gates in `runtime.skill_package`.

## Execution Backend Boundary

LoopPlane schedules agent roles, durable state transitions, validation,
recovery, and generic background-command supervision. It does not infer a
workload backend from task prose, intercept launcher binaries, rewrite command
search paths for backend telemetry, choose machine resources, or enforce
backend-specific launch deadlines.

Environment access, resource selection, launch commands, storage placement,
and site policy belong to each project's `SHARED_CONTEXT.md`, runner local
configuration, or optional project skills. This boundary lets the same
LoopPlane package move among personal machines, shared compute environments,
and hosted servers without source changes.

`runtime.skill_package` implements the `loopplane skill` command group used by the
portable package workflow. It shares package diagnostics with
`scripts/check_package_tree.py`, delegates project-local installation to the
same initialization path used by `loopplane init`, records flat compatibility
workspace metadata, preserves project-owned runtime state during updates, and
validates package metadata and contents before creating a pack artifact.

`runtime.validation` implements the agent-native authoritative validator used
by `loopplane validate`. It reads `PLAN.md`, worker run evidence, logs, artifacts,
commands, captured diffs, and `agent_status.json`, then writes validator-owned
`validation.json` and `validator.log` for the run directory. Structural
violations can still block acceptance, while brittle text and command matching
clauses are advisory warnings so agent judgment is not overridden by fragile
parsing. Candidate task IDs from `agent_status.evidence_satisfies` are
independently validated and recorded for the reconciler, but worker claims never
directly update `PLAN.md`.

`runtime.reconciliation` implements `loopplane reconcile`. It consumes
authoritative `validation.json`, marks only accepted task IDs complete in
`PLAN.md`, writes `latest.json` pointers for those accepted IDs, records
validation failures or approval requests, appends runtime events, and writes a
pending read-model rebuild request.

`runtime.workflow_lifecycle` owns v1.6 workflow registry mutations. It records
workflow create, archive, restore, fork, import, completion, and supersede
transitions in `.loopplane/workflow_registry.json`, preserves older workflow
records, and updates `.loopplane/current_workflow.json` only when a caller performs
an explicit current/active workflow change.

`runtime.migration_export` implements `loopplane export --profile
source|stateful|archive`. It creates a deterministic tar-format project archive
with an embedded manifest, uses zstd for `.tar.zst` output when available, and
falls back to uncompressed tar at the requested path when zstd tooling is
unavailable. Exports are scoped to the project workspace boundary and exclude
process state, locks, leases, dashboard secrets/server state, machine-local
runner overrides, derived read models, `LOOPPLANE_HOME` files, and sibling
workspaces.

`runtime.migration_import` implements `loopplane import <archive> --target
<project>` for stateful archives and `loopplane import <archive> --target
<project> --read-only` for archive-profile imports. It validates manifests,
hashes, archive member safety, and schemas before restoring project-local
workflow truth into an absent or empty target. Stateful import regenerates clean
local runtime scaffolding and leaves agent runner configuration, read-model
rebuilds, health checks, and resume decisions as explicit post-import steps.
Read-only archive import marks imported registry records as
`read_only_imported`, regenerates non-resumable runtime state, and relies on
dashboard/control mutability checks to prevent accidental mutation.

`runtime.loopplane_home` owns the machine-local `LOOPPLANE_HOME` resolver. The default
is `~/.loopplane`; the `LOOPPLANE_HOME` environment variable overrides it for discovery
and local machine state. Its initializer creates the v1.6 discovery scaffold
without overwriting existing machine-local files: `config.json`,
`registry/workspaces.json`, `runners/agent_runners.local.json`,
`dashboard/servers.json`, `locks/runner_locks/`, `locks/dashboard_locks/`,
`package_cache/`, and `logs/`. Generated global workspace registries carry
`authority: "discovery_only"` and are snapshots for discovery/display only.
Dashboard startup records a token-free machine-local discovery entry in
`$LOOPPLANE_HOME/dashboard/servers.json` while keeping the dashboard token and
workflow-specific server state under the selected workflow's project-local
`runtime_dir`. Project-local `.loopplane/` files remain the source of workflow
truth.

Agent runner loading starts from the portable project-local
`agent_runners.json` for runner IDs, roles, and policy defaults, then overlays
machine-local runner settings from `$LOOPPLANE_HOME/runners/agent_runners.local.json`
and `.loopplane/config/local/agent_runners.local.json`. Local overrides can carry
executable paths, auth probes, env values, and other non-portable settings, but
they cannot introduce runner IDs absent from portable config.
Runner records can include an optional `resource_policy`. The resolved policy is
deep-merged through inheritance and local overrides, and a present resolved
policy must provide `global_concurrency_limit`, `lock_scope`, `lock_key`, and
`queue_when_busy`. `lock_scope` currently accepts `machine` and `workspace`;
machine-scoped locks are intended to live under
`$LOOPPLANE_HOME/locks/runner_locks/<lock_key>.lock` without replacing the
project-local scheduler locks. They carry a unique owner ID, host/process
identity, active-run lease path, and a 30-second heartbeat with a 120-second
TTL. The owner holds a lifetime POSIX advisory lock on the metadata inode, so
heartbeat, release, and stale reclaim cannot replace or unlink a newer owner.
Platforms or shared filesystems without advisory locking support fail closed.
Waiters poll once per second and may reclaim a lock only when the advisory owner
is gone and its active-run lease is terminal/expired or its host-aware heartbeat
is stale.

`runtime.scheduler.append_event` writes append-only JSONL records under the
resolved workflow `runtime_dir` through `event_append_lock`. Runtime events
include monotonic `seq`/`sequence`, deterministic `event_id`, previous event
ID/hash-chain links, and a canonical `event_hash`. Event appends are flushed
with `fsync`. Snapshot helpers write compact event projections under the
resolved workflow snapshot directory, load the latest snapshot, and replay
only later events so later read-model work does not need to scan the whole log.
Stable scheduler wait states are coalesced: an unchanged wait writes at most one
audit event per five minutes, and heartbeat-only timestamp changes do not defeat
coalescing. Detached supervisors wait on lightweight file-stat changes between
scheduler ticks instead of rebuilding the scheduler snapshot every second.

`runtime.read_models` implements `loopplane rebuild-read-models`. It parses
`PLAN.md`, reads latest pointers and validator-owned `validation.json` files,
replays event state from the latest snapshot plus later event records, writes
the required dashboard read models under the configured read-model directory,
rebuilds sanitized version-control status, and validates the generated model
shapes before writing them. The builder treats read models as derived output
and does not mutate authoritative plan, validation, runtime state, or event
log files.
Event graph context and run-metadata discovery are bounded. Artifact/cache
directories are pruned from control-plane scans, and node/validation/status
metadata is discovered once per rebuild rather than through repeated recursive
globs. Human summaries are on-demand by default and fingerprint bounded artifact
metadata instead of hashing every artifact payload.

Git checkpoint creation uses scoped repository probes, a temporary index, and
managed refs. Default result trees are excluded, postcondition safety is
guaranteed by mechanical isolation rather than repeated whole-tree scans, and
each checkpoint has time/path/byte budgets. High-frequency before-worker and
after-validation checkpoints are disabled by default; if explicitly enabled,
budget exhaustion is non-blocking and recorded as `skipped_budget`.
Existing workflows do not need a migration to gain the hard limits or result
tree exclusion: missing `checkpoint_limits` use the bounded defaults and the
runtime exclusion also protects older configs. An older workflow that explicitly
keeps high-frequency checkpoint flags enabled may set both flags to `false` to
adopt the new low-frequency policy.

Planner workspace context, adapter output discovery, validator input discovery,
inspector metadata, objective artifact inventory, and nested-repository boundary
observation all use bounded traversal. A boundary observation that cannot finish
inside its scan budget reports `out_of_boundary_watch_budget_exceeded` instead of
holding an agent run while walking an arbitrarily large sibling tree.

Normal `health` probes are bounded operational checks because dashboards and
watchdogs call them frequently: they inspect recent event/checkpoint history,
the latest managed checkpoint ref, and bounded result metadata. `health --strict`
is the explicit full-history audit that revalidates every event-chain record,
checkpoint record/ref, and complete JSONL read model.

Worker output has two different lifetimes. Durable project deliverables can live
in task-appropriate project folders such as `data/`, `artifacts/`, reports, or
named subproject directories when the plan asks for them. Per-run evidence,
handoff notes, logs, `agent_status.json`, and validation support files should
remain under the role output directory so scheduler recovery, validation, and
read models can trace them without treating transient run state as product data.

`runtime.dashboard` implements `loopplane dashboard`. It renders a static,
read-only dashboard bundle from read models and compares their event references
and event source hashes with the current event-log projection. Stale renders
surface machine-readable `read_model_freshness` metadata and a visible rebuild
command hint; only the explicit `--rebuild-read-models` option refreshes the
derived read-model files before rendering.
Dashboard server mode is required for request-entry controls; static generation
does not append planning, execution, approval, rebuild, chat, or change-request
records.

`runtime.detached` implements the detached supervisor used by
`loopplane start --detach`. The CLI records a `start` control request, starts
`python -m runtime.detached supervisor --project <project>` in a new local
process session where supported, and returns to the caller. The supervisor
then runs one scheduler tick at a time, performs validator/reconciler
follow-up after worker runs, and keeps polling through recoverable wait states
such as paused, waiting for config, waiting for approval, and waiting for
background jobs. It exits on completion, explicit stop, unrecoverable
attention, scheduler failure, or failed validation/reconciliation follow-up.
During controller replacement it retries a pre-existing scheduler-instance lock
for a bounded 150-second startup grace, allowing the old 120-second lease to
expire without requiring a second manual submission.

Each detached launch copies the current `runtime/` and `templates/` trees into
a content-addressed directory under the workflow runtime directory and starts
the supervisor from that snapshot. This keeps imported Python and prompt
templates on one generation even if the installation checkout is updated while
the controller is alive. `supervisor.json` records the snapshot fingerprint,
path, manifest, source checkout, and file count. The supervisor compares that
immutable baseline with the mutable source checkout; when tracked runtime or
template content changes, it materializes a new verified snapshot and replaces
itself from that snapshot before running another scheduler tick. Snapshot reuse
rejects missing, extra, symlinked, or manifest-mismatched files.

Detached supervisor metadata is stored in the configured runtime directory as
`supervisor.json`; the default flat compatibility path is
`.loopplane/runtime/supervisor.json`. The record includes the workflow ID, project
root, command, PID/process handle, process birth identity, host, runtime source
fingerprint, start/update/heartbeat timestamps, compact last scheduler result,
last follow-up result, log paths, and terminal exit status. Supervisor stdout and stderr are written under
the resolved runtime supervisor log directory. `runtime.control` reads this
metadata for `loopplane status`, `loopplane attach`, and `loopplane logs`, and classifies
active metadata as stale when the PID is dead or reused, the heartbeat is old,
the workflow/source identity differs, or required active fields are missing.

Control commands are durable request records, not direct state mutations.
`pause`, `resume`, `stop`, dashboard control buttons, and the detached
`start` path append to `control_requests.jsonl`; scheduler ticks append the
corresponding `control_responses.jsonl` records after applying or rejecting the
request at a safe point. Mutating control requests are refused before append
when the selected workflow is archived or `read_only_imported`. `resume` also
checks detached supervisor metadata and can relaunch the supervisor for stopped
or stale detached workflows whose runtime state remains recoverable.

Background job coordination is scheduler-owned. Workers report
`status=running_background`, `next_prompt_ready=false`, and wake conditions in
their run-local `agent_status.json`; the scheduler persists those records to
`background_jobs.json` under the resolved runtime directory, normalizes
allowed statuses, refreshes heartbeats, and waits before starting more work
while continuation is unsafe.
Workers can also start long-running commands with `loopplane background start --
<command>`. That command launches a LoopPlane supervisor, records the job in
`background_jobs.json`, maintains heartbeat/log/exit-code state, and returns an
`agent_status_fragment` workers can copy into `agent_status.json`. Long or
failure-prone background jobs can opt into cheap process/log probes with
`--watchdog-interval-seconds`. Semantic inspector checks are separately throttled
with `--watchdog-agent-interval-seconds` (7200 seconds by default),
`--watchdog-runner`, and `--watchdog-question`; the supervisor records recent
check summaries and can stop treating the job as healthy when the inspector
recommends recovery. Ordinary jobs shorter than the agent interval therefore
incur no LLM watchdog call.
`loopplane health` treats malformed, stale, failed, timed-out, or recovery-needed
background job records as degraded runtime state. It also inspects configured
machine-level runner lock keys under `$LOOPPLANE_HOME/locks/runner_locks/` and
reports stale or malformed lock files with recovery guidance without mutating
project-local workflow truth.

`runtime.approval` implements the human approval file protocol used by the
scheduler and CLI. `security.approval.enabled` defaults to `false`; in that
mode approval-required work is blocked as `requires_attention` instead of
being silently approved. When approvals are enabled, the scheduler writes
pending records under the resolved runtime directory, and `loopplane approve` /
`loopplane reject` append decisions to the matching runtime approval response log.
