# LoopPlane Scripts

This directory contains package command entrypoints and development validation
helpers.

The `loopplane` CLI entrypoint exposes the LoopPlane command surface. Implemented
commands include skill package doctor/install/update/pack workflows, project
initialization, planning, scheduling, validation, reconciliation, health,
read-model rebuilds, static and server dashboard modes, version-control
helpers, `workspace current`, `workspace register`, `workspace unregister`,
`workspace scan`, `workspace list`, `workspace doctor`,
the top-level `workflow` history command group, `workflow list`,
`workflow current`, `workflow show`, `workflow switch`, `workflow create`,
`workflow archive`, `workflow restore`, `workflow fork`, `dashboard --workflow`,
`dashboard --port auto`, `dashboard list`, `export --profile source
--output <archive>`, `export --profile stateful --output <archive>`,
`export --profile archive --output <archive>`, `import <archive> --target
<project>` for stateful archives, `import <archive> --target <project>
--read-only` for archive-profile dashboard-only imports, and human approval
inspection or response commands.

Static dashboard mode is an offline, read-only snapshot; server dashboard mode
is required for request-entry controls. `noop` and `shell` command examples are
smoke fixtures, while full provider-backed agent execution uses `codex_cli` or
`claude_code_cli` with installed and authenticated CLIs.

Package helper scripts are thin wrappers around the same CLI handlers:

- `scripts/doctor.sh` runs `python3 scripts/loopplane skill doctor`.
- `scripts/install_local.sh` runs
  `python3 scripts/loopplane skill install --target <project>`.

Agent-driven installs must treat `skill install` as incomplete unless runner
readiness is `ok`. If install/update reports `*_waiting_config` or
`runner_readiness: waiting_config`, first discover the required external CLI
paths with `command -v codex` or `command -v claude`, configure the absolute
path with `loopplane configure-agent`, and rerun `loopplane doctor-agent` before
planning, starting, or resuming a workflow.

Approval commands include:

- `loopplane approvals --project <path>`
- `loopplane approve <approval_id> --project <path>`
- `loopplane reject <approval_id> --project <path>`

## v1.6 Support Status

The implemented v1.6 command surface includes same-workspace workflow history,
dashboard workflow switching, workspace registry/current workflow pointer
inspection and mutation controls, workspace and workflow CLI groups,
LOOPPLANE_HOME discovery/local override commands, runner resource lock health
inspection, monorepo and nested workspace boundary enforcement, migration
export/import profiles, Git-ref bundle export/import, and archived/read-only
workflow mutation safeguards.

`workspace register`, `workspace unregister`, `workspace scan`, `workspace
list`, and `workspace doctor` use `$LOOPPLANE_HOME/registry/workspaces.json` only
as a discovery index. Project-local `.loopplane/workspace.json`,
`.loopplane/workflow_registry.json`, and `.loopplane/current_workflow.json` remain
authoritative for workflow truth. Dashboard server discovery records are
similarly token-free convenience records under `$LOOPPLANE_HOME/dashboard/`.

Migration commands support source, stateful, and archive export; stateful
import; read-only archive import; stale-state exclusion for locks, leases,
PIDs, dashboard tokens/server state, runner secrets, and LOOPPLANE_HOME files; and
Git-ref bundle export/import through `loopplane vc export --output <bundle>` and
`loopplane vc import <bundle>`.

Release validation under `scripts/check_package_tree.py` gates the v1.6
support status above, including same-workspace history, workspace
registry/current pointer support, v1.5 flat compatibility, canonical workflow
roots, dashboard switching, archived/read-only mutation rejection, LOOPPLANE_HOME
authority separation, and migration stale-state exclusion.

Commands outside the implemented MVP remain limited to the intentionally
deferred scope in `LoopPlane.md` 26.2 or explicit extension points such as global
cross-workspace dashboard discovery, parallel worker execution, cloud
backends, advanced graph editing, multi-user dashboard authentication, and
custom or terminal-capable adapter integrations.
