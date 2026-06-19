# LoopPlane

LoopPlane is a standalone CLI runtime and portable skill package for durable,
plan-driven agent workflows. A user brief becomes a project-local `.loopplane/`
workflow instance, a living workflow-local `PLAN.md`, scheduler-controlled CLI-agent runs,
durable evidence, validation, local Git checkpoints, read models, and a static
dashboard.

## For Human Users

If you want an external coding agent to use LoopPlane for a real project, start
with the short product overview in [`LOOPPLANE_SHOWCASE.md`](LOOPPLANE_SHOWCASE.md)
and the copy-paste prompts in [`README_FOR_HUMANS.md`](README_FOR_HUMANS.md).

The intended human workflow is simple:

1. Ask your agent to install LoopPlane into the target workspace and configure
   the available Codex or Claude Code CLI runner.
2. Give the agent your requirements document and ask it to initialize a
   LoopPlane plan from those requirements.
3. Review the generated tasks and objectives, preferably in the dashboard.
4. Ask the agent to activate the approved plan and start the LoopPlane workflow
   in the background.
5. Watch the dashboard while the workflow runs; the agent loop writes evidence,
   checks objectives, expands follow-up work when needed, and leaves a
   reviewable project-local record.

In practice, LoopPlane lets you delegate the repetitive execution loop while
keeping the plan, evidence, objective status, and dashboard visible enough to
review.

The runtime follows the invariant from `LoopPlane.md`: plan intent, evidence,
validation, reconciliation, Git checkpoints or approved exceptions, and final
verification must all agree before a workflow is complete.

## Implementation Status

`LoopPlane.md` is the authoritative product specification. This repository
currently implements the standalone MVP runtime surface and the v1.6 support
status called out below. `LoopPlane.md` section 26 remains the release scope
authority, including intentionally deferred MVP items.

Completed standalone/MVP functionality includes real `codex_cli` and
`claude_code_cli` adapter execution, detached supervisor-backed runtime,
skill doctor/install/update/pack workflows, `write-brief`, local Git
checkpoint and `vc` inspection/rollback commands, read-model rebuilds,
static and token-protected server dashboard modes including auto port
allocation, request-record dashboard controls, inspector chat/change requests,
workspace-scoped `dashboard list` inspection, validation, reconciliation,
final verification, and release gates that block unfinished required command
and adapter surfaces.

### v1.6 Support Status

Implemented v1.6 support includes same-workspace workflow history and
dashboard workflow switching. The dashboard selector can visualize registered
workflow histories in the current workspace without moving
`.loopplane/current_workflow.json`, and archived/read-only workflow mutation
safeguards reject mutating controls unless the workflow is explicitly restored
or forked.

Workspace identity is project-local. Standalone `loopplane init` creates
`.loopplane/workspace.json`, `.loopplane/workflow_registry.json`, and
`.loopplane/current_workflow.json` plus a current canonical workflow root at
`.loopplane/workflows/<workflow_id>/`. The authoritative active plan for canonical
workflows is `.loopplane/workflows/<workflow_id>/PLAN.md`; root `PLAN.md` is not
projected by default to avoid confusing active workflow history. The skill
install path keeps the v1.5 flat compatibility layout whose `workflow_root` is
`.loopplane`.

The workspace and workflow CLI groups are implemented for current/register/
unregister/scan/list/doctor plus workflow list/current/show/switch/create/
archive/restore/fork. The runtime has a registry lifecycle API for workflow
create, archive, restore, fork, import, completion, and supersede transitions,
and it updates `.loopplane/current_workflow.json` only for explicit current/active
workflow changes.

LOOPPLANE_HOME discovery and local override support is implemented as a
machine-local convenience layer. `$LOOPPLANE_HOME/registry/workspaces.json` and
`$LOOPPLANE_HOME/dashboard/servers.json` are discovery indexes only and never
override project-local `.loopplane/` workflow truth. `configure-agent` stores local
runner overrides under `$LOOPPLANE_HOME/runners/agent_runners.local.json`, while
portable runner defaults remain project-local.

Runner resource locks are supported through runner `resource_policy` records
and machine-scoped lock files under `$LOOPPLANE_HOME/locks/runner_locks/`.
Scheduler locks remain project-local, and health checks report stale or
malformed runner locks without mutating workflow truth.

Monorepo and nested workspace boundaries are supported. Workspace identity
keeps project root, repository root, and workspace boundary separate; workers,
validation, reconciliation, Git checkpoints, and CLI selection enforce the
configured boundary policy, and nested LoopPlane instances require an explicit
target signal.

Migration export/import profiles and Git-ref bundle export/import are
implemented. Source, stateful, and archive exports exclude stale machine-local
state; stateful import restores portable workflow truth into an absent or
empty target; read-only archive import marks workflows `read_only_imported`;
`vc export` and `vc import` handle only LoopPlane-managed checkpoint refs without
switching branches or rewriting user history.

Release gates now cover v1.6 schema files, JSON/JSONL examples, stale
`schema_version: "1.5"` emissions, same-workspace history switching, workspace
registry/current pointer support, v1.5 flat compatibility, canonical v1.6
workflow-root mode, dashboard workflow switching, archived/read-only mutation
rejection, LOOPPLANE_HOME authority separation, and migration stale-state
exclusion.

Intentionally deferred v1.6 scope is limited to `LoopPlane.md` 26.2 MVP deferrals
and optional extension points: multiple concurrently running workflows per
workspace, global cross-workspace dashboard discovery, parallel worker
execution, cloud deployment, Kubernetes or Temporal backends, advanced graph
editing, full semantic LLM validation, complex cost accounting, multi-user
dashboard authentication, remote browser collaboration, optional cross-agent
files such as `CLAUDE.md` or `.codex/`, and custom or terminal-capable adapter
extensions.

The project-local flat compatibility layout remains supported. Initial install
and first init create `.loopplane/workspace.json`,
`.loopplane/workflow_registry.json`, and `.loopplane/current_workflow.json` for one
flat workflow whose `workflow_root` is `.loopplane`; the dashboard can render
registry entries that have read models.
`loopplane init` is create-only and refuses an existing workspace marker such as
`.loopplane/workspace.json`, `.loopplane/config/instance.json`, or
`.loopplane/config/workflow.json` with guidance to inspect, attach, resume, or use
an explicit workflow-history command. The runtime has a registry lifecycle API
for workflow create, archive, restore, fork, import, completion, and
supersede transitions, and it updates `.loopplane/current_workflow.json` only for
explicit current/active workflow changes. The canonical
`.loopplane/workflows/<workflow_id>/` layout, `loopplane workspace current`
inspection command, and `loopplane workspace register <project>` /
`loopplane workspace unregister <workspace_id>` / `loopplane workspace scan <directory> [...]` /
`loopplane workspace list` / `loopplane workspace doctor` LOOPPLANE_HOME convenience-index
and diagnostic commands are implemented. `LOOPPLANE_HOME` resolves from the
`LOOPPLANE_HOME` environment variable and defaults to `~/.loopplane`. The top-level
`loopplane workflow` history command group, read-only `loopplane workflow list`,
`loopplane workflow current`, `loopplane workflow show <workflow_id>`, and explicit
`loopplane workflow switch <workflow_id>` current-pointer control command are
implemented. `loopplane workflow create --brief "..."` allocates a new canonical
`.loopplane/workflows/<workflow_id>/` root, appends the workspace-local registry,
and moves the current pointer only after runtime safety checks pass.
`loopplane workflow archive <workflow_id>` marks only the selected project-local
registry record archived, preserves workflow roots and prior history, rejects
read-only, already archived, malformed, and active/running workflow states, and
does not move `.loopplane/current_workflow.json`. `loopplane workflow restore
<workflow_id>` restores only archived, mutable workflow histories, rejects
read-only and non-archived histories, checks active runtime conflicts, preserves
workflow roots and prior history, and moves `.loopplane/current_workflow.json` only
after safety checks pass. `loopplane workflow fork <workflow_id> --name "new attempt"`
creates a new canonical workflow root from a selected source, records fork
lineage without mutating the source history, allows archived/read-only sources
as the explicit escape path, checks active runtime conflicts, and moves
`.loopplane/current_workflow.json` only after safety checks pass. `loopplane vc export
--output <bundle>` is implemented for LoopPlane-managed checkpoint refs.
`loopplane export --profile source --output <archive>`, `loopplane export --profile
stateful --output <archive>`, and `loopplane export --profile archive --output
<archive>` are implemented for project-local migration archives. Source export
includes files under the workspace boundary, sanitized portable config,
workflow registry/current metadata, evidence summaries, runtime events, and
Git checkpoint metadata.
Stateful export additionally preserves workflow snapshots, failure registry,
historical results, and final verification reports. Archive export preserves
the same visualization history as stateful export and adds read-only import
intent metadata for later dashboard-only viewing. All export profiles exclude
locks, leases, dashboard tokens/server state, derived read models,
machine-local runner overrides, stale process handles, `LOOPPLANE_HOME` files, and
sibling workspaces.
`loopplane import <archive> --target <project>` is implemented for stateful
migration archives, and `loopplane import <archive> --target <project>
--read-only` is implemented for archive-profile imports. Both modes validate
the archive manifest, member hashes, path boundaries, and project schemas before
restoring into an absent or empty target. Stateful import preserves workflow
identity, registry/current pointers, events, evidence, Git checkpoint metadata,
and final reports, regenerates clean local runtime scaffolding without stale
process handles, and reports post-import steps for `doctor-agent --all`,
`configure-agent`, `rebuild-read-models`, `health`, and manual `resume` when
appropriate. Read-only archive import marks imported workflows as
`read_only_imported`, regenerates non-resumable runtime state, omits resume
guidance, and leaves the dashboard available for visualization.

Optional behavior allowed by the spec but not required for the current core
runtime includes multiple active-running workflows in one workspace, global
cross-workspace dashboard discovery, parallel workers, cloud or
Kubernetes/Temporal backends, advanced graph editing, full semantic LLM
validation, complex cost accounting, multi-user dashboard authentication,
remote browser collaboration, optional cross-agent files such as `CLAUDE.md`
or `.codex/`, and custom or terminal-capable adapter extensions.

Smoke and fixture paths are intentionally narrower than the full
provider-backed flow. `noop`, `shell`, fake `codex`/`claude` test binaries,
planner/auditor-disabled flows, and offline/static dashboard bundles are for
local smoke, fixture, or read-only validation; they do not replace installed
and authenticated `codex_cli`/`claude_code_cli` execution, server-mode
request-entry dashboard behavior, or the full scheduler/validator/reconciler/
checkpoint/final-verifier path.

## Install

Use the package directly from this repository:

```bash
python3 scripts/loopplane --help
```

For shell convenience, add the repository `scripts/` directory to `PATH` or
symlink `scripts/loopplane` somewhere already on `PATH`.

LoopPlane itself uses only the Python standard library for the core runtime.
Schema-validation tests and strict schema checks use `jsonschema`; this repo's
test commands install it through `uv --with jsonschema`.

### Agent-Assisted Install Readiness

When an agent installs LoopPlane into a project, installation is not considered
complete until required external CLI runners have been found, configured, and
doctored. The install command safely checks for both Codex CLI and Claude Code
CLI. It binds the first available default harness, preferring Codex when both
are present, to the generic `worker` runner and writes discovered absolute paths
to the project-local machine override file
`.loopplane/config/local/agent_runners.local.json`. It then runs runner doctor
checks.

If `skill install` or `skill update` reports `installed_waiting_config`,
`attached_waiting_config`, `updated_waiting_config`, `current_waiting_config`,
or `runner_readiness: waiting_config`, the installing agent must resolve CLI
discovery, authentication, or runner configuration before running `plan`,
`activate-plan`, `start`, or `resume`.

The setup agent should first try local discovery:

```bash
CODEX_BIN="$(command -v codex || true)"
CLAUDE_BIN="$(command -v claude || true)"
```

Use Codex for the generic default worker runner; planner, auditor, validator,
summary, final reviewer, and inspector inherit that runner unless explicitly
overridden:

```bash
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner worker --role worker --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner planner --role planner --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner auditor --role auditor --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner worker
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner planner
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner auditor
```

Use Claude Code for the generic fallback worker runner, or bind it to `worker`
when Claude is the available default harness:

```bash
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner worker_fallback --role worker --adapter claude_code_cli --command "$CLAUDE_BIN"
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner worker_fallback
```

Do not reinstall Codex or Claude Code automatically unless the user explicitly
approves installation. A found-but-unauthenticated CLI should be treated as an
authentication setup issue and surfaced through `doctor-agent`, not as a reason
to overwrite the user's existing installation.

## Skill Package Workflows

Package diagnostics, project-local installation, local updates, and portable
bundle creation are available through the `skill` command group:

```bash
python3 scripts/loopplane skill doctor
python3 scripts/loopplane skill install --target "$PROJECT"
python3 scripts/loopplane skill update --target "$PROJECT"
python3 scripts/loopplane skill pack --output ./dist/loopplane-skill.zip
```

`skill doctor` validates the package before installation or release. It checks
the required portable layout, executable package scripts, non-empty metadata and
source files, `skill.json` package metadata, required command handlers, required
adapter implementations, and completed-requirement docs wording. The shell
wrapper is equivalent:

```bash
scripts/doctor.sh --json
```

`skill install --target <project>` creates or attaches a runnable
project-local workflow instance and then checks install-time runner readiness.
On an empty target it materializes the current flat compatibility layout:

```text
project/
  .codex/
    skills/
      loopplane/
        SKILL.md
        agents/openai.yaml
        references/
        scripts/
        runtime/
  .claude/
    skills/
      loopplane/
        SKILL.md
        references/
        scripts/
        runtime/
  PROJECT_BRIEF.md
  PLAN.md
  .loopplane/
    SHARED_CONTEXT.md
    workspace.json
    workflow_registry.json
    current_workflow.json
    config/
      workflow.json
      agent_runners.json
      dashboard.json
      security.json
      version_control.json
      schema_version.json
      instance.json
      workflow_defaults.json
      local/.gitignore
    planning/
    runtime/
    read_models/
    requests/
    results/
```

The install path uses the supported compatibility profile
`layout: compatibility_flat` with `workflow_root: .loopplane`. It does not create
canonical `.loopplane/workflows/<workflow_id>/` workflow histories during initial
flat install; use `workflow create`, `workflow fork`, or migration/import
commands for canonical workflow roots. If the target already contains `.loopplane/config/workflow.json`,
install attaches to the existing flat instance, preserves matching metadata,
and reports `status: attached`. If installation would overwrite an existing
different project file, it refuses with conflicts instead of partially
rewriting the project. The shell wrapper runs the same install command:

```bash
scripts/install_local.sh --target "$PROJECT" --json
```

The `.codex/skills/loopplane/` and
`.claude/skills/loopplane/` directories are project-local
Agent Skills projections with directory names matching the shared `SKILL.md`
frontmatter `name`. They include the supporting `references/`, `scripts/`,
`runtime/`, `templates/`, `dashboard/`, and `examples/` files so both Codex and
Claude Code can discover the same skill without dangling relative references.
`agents/openai.yaml` is included for Codex UI metadata and is harmless in the
Claude Code projection.

For agent-driven setup, `status: installed` or `status: attached` means schema,
Git, and required runner readiness all passed. A `*_waiting_config` status means
the project files were materialized or attached, but the installing agent still
must find and configure required Codex or Claude Code CLI paths and pass
`doctor-agent` before continuing.

`skill update --target <project>` requires an existing
`.loopplane/config/workflow.json`. It refreshes LoopPlane-managed package metadata,
creates missing flat compatibility workspace/current-workflow metadata, and
writes `.loopplane/config/package_manifest.json` when needed. It preserves the
project brief, active plan, shared context, runtime state, control and approval
records, read models, requests, results, checkpoints, logs, and
`.loopplane/config/local/` files. A second update of an already current project is
idempotent and reports `status: current`.

`skill pack` runs the package doctor first, validates package metadata and
required contents, then creates a deterministic ZIP bundle. By default it
writes `dist/<package>-<version>.zip`; `--output` may name a file or an
existing output directory. The packer selects only package metadata and roots
declared by `skill.json`, so project-local `.loopplane` runtime artifacts are not
part of the source set. It excludes VCS directories, build/dist output,
dependency caches, development prompt queues, Python bytecode/cache files, and
temporary editor files.

All skill commands support text output for humans and `--json` for automation.
Text output reports status, target or artifact path, workflow identity where
applicable, created/updated/preserved counts, validation state, warnings, and
conflicts. JSON output includes stable `ok`, `status`, `schema_version`,
path, identity, validation, warning, error, and conflict fields, plus
command-specific fields such as `created`, `preserved`, `protected_paths`,
`artifact_sha256`, `included_files`, and `excluded_roots`.

## Quick Start

Create a local workflow instance from this repository checkout:

```bash
PROJECT=/tmp/loopplane-demo
python3 scripts/loopplane init --project "$PROJECT" --brief "Build and verify a tiny local artifact."
```

Configure real CLI agent runners. The default generated workflow uses generic
runner IDs (`worker`, `planner`, `auditor`, and so on); the `adapter` field binds
those workflow roles to Codex CLI, Claude Code CLI, shell, or another harness.
The installer discovers both Codex and Claude and binds the first available
default harness to `worker`, preferring Codex when both are present. Manual
`configure-agent` writes the local command and diagnostics to the resolved
`$LOOPPLANE_HOME/runners/agent_runners.local.json` file. Portable workflow
defaults remain in project-local `.loopplane/.../config/agent_runners.json`:

```bash
CODEX_BIN="$(command -v codex)"
python3 scripts/loopplane configure-agent --project "$PROJECT" --role worker --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane configure-agent --project "$PROJECT" --role planner --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane configure-agent --project "$PROJECT" --role auditor --adapter codex_cli --command "$CODEX_BIN"
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner worker
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner planner
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner auditor
```

Generate, optionally audit, and activate a plan:

```bash
python3 scripts/loopplane plan --project "$PROJECT"
python3 scripts/loopplane audit-plan --project "$PROJECT"
python3 scripts/loopplane activate-plan --project "$PROJECT"
```

Start the durable scheduler, then inspect status, logs, and the local dashboard
server:

```bash
python3 scripts/loopplane start --project "$PROJECT" --detach
python3 scripts/loopplane status --project "$PROJECT"
python3 scripts/loopplane logs --project "$PROJECT"
python3 scripts/loopplane dashboard --project "$PROJECT" --port 3766
```

LoopPlane starts a local dashboard by default for normal workflow startup and writes
the access link to `LOOPPLANE_DASHBOARD.url` in the workspace. Set
`LOOPPLANE_AUTO_DASHBOARD=0` when a run should skip automatic dashboard startup.

For an offline snapshot instead, omit `--port`:

```bash
python3 scripts/loopplane dashboard --project "$PROJECT"
```

To configure a Claude Code fallback worker runner, install and authenticate the
Claude CLI, ensure `claude --version` succeeds, then configure and doctor
`worker_fallback`:

```bash
CLAUDE_BIN="$(command -v claude)"
python3 scripts/loopplane configure-agent --project "$PROJECT" --role worker --adapter claude_code_cli --command "$CLAUDE_BIN"
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner worker_fallback
```

This configures the real `claude_code_cli` adapter. To bind the generated
default worker runner to Claude without editing JSON, configure `worker`
explicitly:

```bash
CLAUDE_BIN="$(command -v claude)"
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner worker --role worker --adapter claude_code_cli --command "$CLAUDE_BIN"
python3 scripts/loopplane doctor-agent --project "$PROJECT" --runner worker
```

Run `doctor-agent --all` only after every configured runner you expect to check
is installed and authenticated.

For an offline release smoke without external agent CLIs, keep the local noop
and shell adapters as deterministic smoke fixtures rather than production agent
substitutes. For full provider-backed validation, use the real `codex_cli` or
`claude_code_cli` flow above with installed and authenticated CLIs, then run
the release checks in the validation section:

```bash
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner planner --role planner --adapter noop --command noop
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner auditor --role auditor --adapter noop --command noop
python3 examples/minimal_project/write_smoke_plan.py "$PROJECT"
WORKER="$(pwd)/examples/minimal_project/worker.py"
python3 scripts/loopplane configure-agent --project "$PROJECT" --runner worker --role worker --adapter shell --command "python3 $WORKER"
```

For that offline smoke flow, preview the next scheduler action, run one tick,
validate and reconcile the worker evidence, then run final verification:

```bash
python3 scripts/loopplane preview --project "$PROJECT"
python3 scripts/loopplane run --project "$PROJECT" --max-ticks 1
RUN_DIR=$(find "$PROJECT/.loopplane/results/T001/runs" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)
python3 scripts/loopplane validate --project "$PROJECT" --task T001 --run-dir "$RUN_DIR"
python3 scripts/loopplane reconcile --project "$PROJECT" --task T001 --run-dir "$RUN_DIR"
python3 scripts/loopplane final-verify --project "$PROJECT"
```

On success, `final-verify` writes
`.loopplane/runtime/final_verification_report.json`,
`.loopplane/runtime/evidence_manifest.json`, and
`.loopplane/runtime/plan_loop_complete.json`.

## Runtime Commands

Planning commands:

- `init` creates `.loopplane/`, default configs, `PROJECT_BRIEF.md`,
  `.loopplane/SHARED_CONTEXT.md`, initial read models, and local Git state when
  needed. It refuses to run when an existing LoopPlane workspace marker is present,
  preserving workspace identity, workflow history, runtime state, read models,
  and user project files.
- `write-brief` creates or updates the configured project brief from
  `--text`, `--file`, or `--stdin`. Existing different content requires
  `--force`; successful changes append a workflow event, rebuild read models,
  and create a local Git checkpoint when version control is enabled.
- `plan` writes `.loopplane/planning/PLAN_DRAFT.md` and
  `plan_readiness_report.json`.
- `audit-plan` records an optional audit report.
- `revise-plan` runs bounded planner/auditor revision loops.
- `activate-plan` promotes a ready draft to active `PLAN.md` and creates Git
  checkpoints around activation.

Execution and inspection commands:

- `workspace current` reports the project-local workspace identity, current
  workflow pointer, selected registry record, and resolved workflow paths.
  It supports `--json` and safely materializes v1.6 compatibility metadata for
  an otherwise valid v1.5 flat workflow layout.
  For monorepos, `.loopplane/workspace.json` keeps `project_root: "."` for the
  project-local workspace and stores `repo_root` separately as a portable
  project-relative value such as `"../.."`. JSON output includes
  `resolved_project_root`, `resolved_repo_root`, and
  `resolved_workspace_boundary` so tools can distinguish the workspace boundary
  from the enclosing repository root. The default boundary policy is
  `workspace_boundary: "project_root"` with
  `allow_out_of_boundary_writes: false`; older workspace identity files that
  lack these fields are defaulted during compatibility/schema inspection.
  Worker adapters, validation, reconciliation, run diff metadata, and Git
  checkpoints all apply this path policy; shell-family adapters fail a run when
  an external process silently edits a sibling project unless the workspace,
  security config, and active plan explicitly allow that path.
  When parent and child LoopPlane instances are nested, commands that can select,
  mutate, execute, or checkpoint a workspace require an explicit target signal:
  pass `--project <target-project>`, `--workspace-namespace <workspace_id>`,
  or `--allow-nested-workspace` after manually verifying the intended boundary.
  Without one of those signals, the CLI blocks with
  `nested_workspace_requires_explicit_namespace` instead of silently choosing a
  parent or child workspace.
- `LOOPPLANE_HOME` defaults to `~/.loopplane`; set the `LOOPPLANE_HOME` environment
  variable to use a different machine-local discovery directory. Registry
  write commands materialize the recommended machine-local layout:
  `config.json`, `registry/workspaces.json`, `runners/agent_runners.local.json`,
  `dashboard/servers.json`, `locks/runner_locks/`,
  `locks/dashboard_locks/`, `package_cache/`, and `logs/`. Dashboard server
  startup writes token-free discovery entries to
  `$LOOPPLANE_HOME/dashboard/servers.json`; dashboard tokens and workflow-specific
  server state stay project-local. `configure-agent` writes machine-local,
  non-portable runner commands, auth probes, and local environment overrides to
  `$LOOPPLANE_HOME/runners/agent_runners.local.json`; project-local
  `.loopplane/config/local/agent_runners.local.json` is also honored and remains
  gitignored. Re-run `configure-agent` after copying, importing, or moving a
  project to another machine. Runner records may declare an optional
  `resource_policy` with
  `global_concurrency_limit`, `lock_scope`, `lock_key`, and `queue_when_busy`;
  machine-scoped policies reserve lock files under
  `$LOOPPLANE_HOME/locks/runner_locks/`.
- `workspace register <project>` validates project-local workspace metadata and
  upserts one machine-local convenience entry in
  `$LOOPPLANE_HOME/registry/workspaces.json`. Generated registry files include
  `authority: "discovery_only"` to make the schema role explicit. The
  LOOPPLANE_HOME registry is not required for local execution and never overrides
  project-local `.loopplane/` workflow truth.
- `workspace unregister <workspace_id>` removes matching entries from
  `$LOOPPLANE_HOME/registry/workspaces.json` only. It does not delete project-local
  workflow history, rewrite `.loopplane/workspace.json`, alter
  `.loopplane/workflow_registry.json`, or change `.loopplane/current_workflow.json`.
- `workspace scan <directory> [...]` recursively discovers project-local LoopPlane
  workspace identities under one or more directories and rebuilds those scanned slices of
  `$LOOPPLANE_HOME/registry/workspaces.json`. It reports discovered workspace IDs,
  project roots, current workflow IDs, skipped/invalid candidates, and registry
  update details in text or `--json` mode. The scan is read-only with respect
  to project-local `.loopplane/` files, writes only the `discovery_only` global
  index, and skips partial v1.6 or flat compatibility candidates instead of
  materializing missing workspace metadata. If a scan scope contains nested
  parent/child LoopPlane workspaces, it refuses to update the global registry until
  `--allow-nested-workspace` or `--workspace-namespace` is supplied.
- `workspace list` reads `$LOOPPLANE_HOME/registry/workspaces.json` as a
  machine-local convenience index and reports registered workspace IDs, project
  roots, current workflow IDs, stale or missing paths, and read-only
  project-local health in text or `--json` mode. It does not create, delete,
  or rewrite project-local `.loopplane/` workflow truth, and an empty registry is a
  successful no-workspaces result.
- `workspace doctor` runs read-only diagnostics over project-local
  `.loopplane/workspace.json`, `.loopplane/workflow_registry.json`, and
  `.loopplane/current_workflow.json`, then compares
  `$LOOPPLANE_HOME/registry/workspaces.json` as a non-authoritative convenience
  index. It reports invalid local metadata as errors, stale or missing global
  registry entries as warnings, and includes recovery actions in text or
  `--json` output. It does not materialize flat compatibility metadata; use
  `workspace current` intentionally for that compatibility write path.
- `workflow` is a top-level same-workspace workflow-history command group.
  `workflow list` reads the project-local `.loopplane/workflow_registry.json` and
  reports workflow IDs, names, statuses, roots, timestamps, archived/read-only
  labels, summaries, and the current selection marker in text or `--json`
  mode. It is read-only and does not mutate `.loopplane/current_workflow.json`.
  `workflow current` resolves `.loopplane/current_workflow.json` through the
  project-local workflow registry, reports the selected workflow record,
  selection reason, update metadata, workflow root, archived/read-only labels,
  and current marker in text or `--json` mode, and does not mutate
  `.loopplane/current_workflow.json`. `workflow show <workflow_id>` resolves one
  workflow ID through project-local registry truth and reports the selected
  record, current marker, archived/read-only labels, key workflow paths,
  summary/progress, and available read-model freshness metadata without
  mutating registry, pointer, or workflow-local truth. `workflow switch
  <workflow_id>` is the explicit CLI control operation for changing
  `.loopplane/current_workflow.json`; it validates project-local workspace truth,
  rejects archived/read-only targets, checks scheduler locks, active-run
  leases, detached supervisor metadata, and the one-active-running policy, and
  reports text or `--json` output. `workflow create --brief "..."` validates
  project-local workspace truth, refuses empty briefs and active runtime
  conflicts, creates a new canonical workflow root without overwriting earlier
  workflow history, appends `.loopplane/workflow_registry.json`, and updates
  `.loopplane/current_workflow.json` only after safety checks pass. `workflow
  archive <workflow_id>` validates project-local workspace truth, rejects
  read-only and already archived histories, blocks active/running workflow,
  scheduler-lock, active-run lease, and detached-supervisor conflicts, and
  marks only the selected registry record archived without deleting workflow
  roots or moving the current pointer. `workflow restore <workflow_id>`
  validates project-local workspace truth, restores only archived mutable
  histories, rejects read-only and non-archived histories, blocks active/running
  workflow, scheduler-lock, active-run lease, and detached-supervisor conflicts,
  preserves workflow roots, and updates the current pointer only after safety
  checks pass. `workflow fork <workflow_id> --name "new attempt"` validates
  project-local workspace truth, creates a new canonical workflow root with
  source lineage, allows archived/read-only sources as the explicit safe escape
  path, blocks active/running workflow, scheduler-lock, active-run lease, and
  detached-supervisor conflicts, preserves source workflow roots, and updates
  the current pointer only after safety checks pass. Dashboard-only visual
  workflow selection is read-only and must not update the current pointer.
- `preview` shows the next scheduler action without mutating authoritative
  workflow state.
- `start --detach` records a durable start request and launches an independent
  supervisor process with its own scheduler loop. The command returns after the
  supervisor starts; the initiating shell does not need to stay open.
- `run --max-ticks N` executes scheduler ticks.
- `status` reports runtime state, scheduler state, control requests, and
  completion-marker freshness. When detached supervisor metadata exists, it
  also reports supervisor status, PID liveness, heartbeat freshness, and stale
  metadata warnings.
- `attach` shows a bounded snapshot of the active detached supervisor, recent
  runtime events, and supervisor stdout/stderr tails. Use `--follow` with a
  timeout for short polling. `attach --request` keeps the older durable attach
  request behavior for workflows that need the request recorded.
- `logs` tails runtime events, recent control requests/responses, and
  supervisor logs.
- `pause`, `resume`, and `stop` append durable control requests. A detached
  supervisor consumes those requests at scheduler safe points; pause and stop
  do not kill an already running worker by default. `resume` can restart a
  stopped or stale detached supervisor when the workflow state is recoverable.
- `validate` checks a worker evidence run in agent-native advisory mode.
  Structural clauses such as `file_exists` and `schema` can still block
  acceptance, while brittle clauses such as `command_exit_code` and
  `report_contains` are recorded as advisory warnings instead of forcing
  recovery. `file_exists` accepts one path or a comma-separated list, for
  example `file_exists: report.md, README.md`.
- `reconcile` applies accepted validation to `PLAN.md`, latest pointers, event
  logs, failure state, and read-model rebuild triggers.
- `final-verify` proves all active tasks are terminal and backed by latest
  passing validation before writing the completion marker.

Detached runtime operational files live under the configured workflow runtime
directory. In the default flat compatibility layout, that is `.loopplane/runtime/`:

- `supervisor.json` stores supervisor metadata: PID or process handle,
  command, `started_at`, `updated_at`, `heartbeat_at`, log paths, last compact
  scheduler result, and terminal exit status.
- `supervisor/supervisor_stdout.log` and
  `supervisor/supervisor_stderr.log` capture the detached supervisor process.
- `state.json` stores scheduler-owned runtime state, including paused,
  stopped, and detached-request flags.
- `control_requests.jsonl` and `control_responses.jsonl` are the durable
  command channel used by CLI and dashboard controls.
- `events/`, `snapshots/`, `runs/`, `active_run_leases/`,
  `background_jobs.json`, `failure_registry.json`,
  `git_checkpoints.jsonl`, approval JSONL files, and final verification files
  remain project-local operational truth for the active workflow.

Process IDs, heartbeat liveness, dashboard tokens, active locks, leases, and
open process handles are local observations used for supervision and recovery;
they are not portable workflow truth. Stale detached metadata is reported when
the PID is dead, the heartbeat is old, or required active metadata is
incomplete. `status`, `attach`, and `resume` use that classification so a user
can distinguish a completed/stopped supervisor from a recoverable stale runtime
state.

Dashboard and health commands:

- `rebuild-read-models` rebuilds derived read models from authoritative
  runtime files. Add `--workflow <workflow_id>` to rebuild a registered
  workflow's read models without changing `.loopplane/current_workflow.json`.
- `dashboard --project <project>` writes an offline, read-only dashboard
  bundle under `.loopplane/dashboard_static/`; add `--rebuild-read-models` when
  the derived read models should be refreshed before rendering.
- `dashboard --workflow <workflow_id> --project <project>` selects a workflow
  from `.loopplane/workflow_registry.json` for dashboard visualization in static or
  server mode. It loads that workflow's read models and does not update
  `.loopplane/current_workflow.json`.
- `dashboard list --project <project>` inspects the current workspace's
  workflow registry and each workflow's configured
  `runtime/dashboard_server.json` state file plus matching machine-local
  entries in `$LOOPPLANE_HOME/dashboard/servers.json`. It reports workflow labels,
  progress, per-workflow runtime health, completion marker freshness, Git
  checkpoint summary, server URL/host/port/PID, selected workflow ID,
  liveness/staleness, stale-record guidance, and redacted token metadata without mutating
  `.loopplane/current_workflow.json`.
- `dashboard --port 3766 --project <project>` starts a token-protected local
  dashboard server bound to the configured local host, defaulting to
  `127.0.0.1`. Startup output includes the URL, token file path, and server
  state path. In the flat compatibility layout those local files are
  `.loopplane/runtime/dashboard_token` and `.loopplane/runtime/dashboard_server.json`.
  Use `dashboard --port auto --project <project>` to allocate an available
  local port from `.loopplane/config/dashboard.json`, preferring
  `preferred_port` and then skipping occupied ports within `port_range`.
  Server startup also updates `$LOOPPLANE_HOME/dashboard/servers.json` as a
  discovery-only index using the resolved `LOOPPLANE_HOME` environment override
  when present; that index omits token values and token-file paths.
  Workspace read APIs expose `GET /api/workspace`,
  `GET /api/workspace/workflows`, and
  `GET /api/workspace/workflows/<workflow_id>` from project-local workspace
  files. These read routes are visualization-only and do not update
  `.loopplane/current_workflow.json`. `POST /api/workspace/workflows` is the
  explicit workflow-create API; it delegates to `loopplane workflow create`
  semantics and updates workflow registry/current-pointer files only after the
  same safety checks pass. Dashboard control routes write LoopPlane request records
  instead of directly changing `PLAN.md`, scheduler state, event logs,
  validations, read models, or Git state. Mutating routes require the dashboard
  token, enforce same-origin checks, redact sensitive request/response content,
  serve only allowlisted read-model/static files, and reject archived or
  read-only workflow mutation.
  The dashboard never exposes arbitrary shell execution or `.git/` internals.
  Runner configuration defaults to trusted-local mode for local workflows.
  Trusted-local server pages apply model, reasoning effort, prompt delivery,
  timeout, and runner command-name changes to the project-local machine override
  file `.loopplane/config/local/agent_runners.local.json`, so future agents use the
  updated global settings without mutating portable `agent_runners.json`.
  Browser requests still reject environment values, shell fragments, and local
  path commands.
- `health` checks schemas, project-local locks and leases, configured
  machine-level runner locks, background jobs, validation files, failure
  registry, event segments, Git checkpoints, read models, and completion-marker
  freshness. By default it checks the current workflow; add
  `--workflow <workflow_id>` for a registered non-current workflow or `--all`
  for a workspace-wide health summary.

Version-control commands:

- `vc status` reports concise sanitized Git status for the workflow, including
  enabled/available state, repository detection, dirty file count, latest
  checkpoint metadata, and rollback availability.
- `vc doctor` reports Git availability, repository mode, default-on checkpoint
  configuration, and safety settings.
- `vc checkpoint --reason <reason>` creates an isolated managed-ref checkpoint
  without switching branches, pushing, rewriting history, or modifying the
  user's index.
- `vc run-metadata` captures per-run Git metadata on demand. Scheduler-side
  per-worker capture is disabled by default and can be enabled with
  `version_control.json.run_metadata.enabled`.
- `vc diff --task <task>` shows sanitized changed-file summaries, patch
  artifact paths, and before/after checkpoint identifiers from captured task
  run artifacts.
- `vc log` lists sanitized checkpoint metadata from
  `.loopplane/runtime/git_checkpoints.jsonl` for CLI and dashboard inspection.
- `vc rollback --checkpoint <checkpoint>` records an approval-gated rollback
  request with target checkpoint, affected paths, and risk summary; it does
  not mutate the worktree or rewrite history before explicit approval.
- `vc export --output <bundle>` writes a standard Git bundle containing only
  LoopPlane-managed checkpoint refs under `refs/loopplane/<workflow_id>/checkpoints`,
  without pushing, fetching, switching branches, rewriting history, or
  modifying the user's index.
- `vc import <bundle>` imports only LoopPlane-managed checkpoint refs from a
  standard Git bundle, ignores non-LoopPlane refs, and preserves user branches,
  HEAD, and the user's index. It performs no remote push or remote fetch.

## Package Layout

- `SKILL.md` is the skill-style entry instruction.
- `skill.json` contains portable package metadata.
- `agents/openai.yaml` contains Codex skill interface metadata.
- `references/` holds protocol, runtime, planner, adapter, dashboard, and
  security reference documents.
- `templates/` contains project, plan, shared-context, and agent prompt
  templates.
- `scripts/loopplane` is the standalone CLI entrypoint.
- `runtime/` contains the scheduler, validator, reconciler, read-model builder,
  adapters, dashboard renderer, health checks, migrations, and version-control
  support.
- `dashboard/` documents the static dashboard surface.
- `examples/` contains runnable local smoke examples.

## License

LoopPlane is licensed under the Apache License, Version 2.0. See
[`LICENSE`](LICENSE) for details.

## Validation

Common release checks from the repository root:

```bash
python3 scripts/check_package_tree.py
python3 -m unittest tests.test_e2e_smoke tests.test_read_models tests.test_health tests.test_final_verifier
uv run --with jsonschema python -m unittest discover -s tests
git diff --check
```
