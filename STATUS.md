# LoopPlane Status

This page summarizes the implemented product surface for the current standalone
runtime. `LoopPlane.md` remains the authoritative product and protocol
specification.

Current package version: `1.6.0`

Current release tag: `v1.6.0`

## Standalone MVP Surface

The repository currently implements the standalone MVP runtime surface for
durable, plan-driven local agent workflows.

Implemented core capabilities include:

- project-local workflow initialization under `.loopplane/`;
- brief-to-plan planning, audit, revision, and activation flows;
- real `codex_cli` and `claude_code_cli` adapter execution;
- deterministic `noop` and `shell` adapters for local smoke and fixtures;
- scheduler-controlled worker runs with durable runtime events;
- validator and reconciler flows that treat worker output as evidence;
- objective verification reports for phase and final objective gates;
- final verification with evidence manifests and completion markers;
- detached supervisor-backed runtime for background execution;
- pause, resume, stop, attach, status, and logs commands;
- static dashboard generation and token-protected local dashboard server mode;
- read-model rebuilds for dashboard and summary surfaces;
- dashboard request records for planning, control, approvals, inspector chat,
  change requests, and read-model rebuild requests;
- local Git checkpointing through managed refs, plus `vc` inspection,
  diff, rollback-request, export, and import commands;
- skill doctor, install, update, and pack workflows;
- migration export/import profiles for source, stateful, and archive use cases;
- workspace and workflow history commands;
- release gates that block unfinished required command and adapter surfaces.

## v1.6 Support

Implemented v1.6 support includes:

- same-workspace workflow history;
- dashboard workflow switching without implicitly moving
  `.loopplane/current_workflow.json`;
- archived and read-only workflow mutation safeguards;
- project-local workspace identity through `.loopplane/workspace.json`,
  `.loopplane/workflow_registry.json`, and `.loopplane/current_workflow.json`;
- canonical workflow roots under `.loopplane/workflows/<workflow_id>/`;
- v1.5 flat compatibility for skill-install workflows;
- workspace commands for current, register, unregister, scan, list, and doctor;
- workflow commands for list, current, show, switch, create, archive, restore,
  and fork;
- `LOOPPLANE_HOME` discovery and machine-local override support;
- machine-scoped runner resource locks;
- monorepo and nested workspace boundary enforcement;
- source, stateful, and archive migration exports;
- stateful and read-only archive imports;
- Git-ref bundle export/import for LoopPlane-managed checkpoint refs;
- release validation for schema files, examples, history switching, dashboard
  switching, authority separation, migration stale-state exclusion, and
  mutation rejection.

## Authority Model

Project-local `.loopplane/` files remain authoritative for workflow truth.

`$LOOPPLANE_HOME` is a machine-local convenience layer for discovery, local
runner overrides, dashboard server records, and resource locks. It must not
override project-local workflow state.

Dashboard views, read models, summaries, and generated static bundles are
projections over workflow truth. They help humans inspect or operate the
workflow, but they do not replace the plan, event log, validations, objective
reports, or completion marker.

## Deferred Scope

The following remain intentionally deferred or extension-oriented rather than
core standalone MVP commitments:

- multiple concurrently running workflows in one workspace;
- global cross-workspace dashboard discovery;
- parallel worker execution;
- cloud deployment;
- Kubernetes or Temporal backends;
- advanced graph editing;
- full semantic LLM validation for all checks;
- complex cost accounting;
- multi-user dashboard authentication;
- remote browser collaboration;
- optional cross-agent files such as `CLAUDE.md` or `.codex/`;
- custom or terminal-capable adapter extensions.

## Smoke And Fixture Scope

Local smoke paths use deterministic fixtures. `noop`, `shell`, fake
`codex`/`claude` test binaries, planner/auditor-disabled flows, and offline
dashboard bundles are useful for local validation, but they do not replace a
full provider-backed run with installed and authenticated Codex CLI or Claude
Code.

The minimal example in `examples/minimal_project/` is intentionally a local
smoke fixture. It disables the optional semantic final reviewer and writes
deterministic objective verification reports so the final deterministic gates
can be exercised without external agent credentials.

For production-like validation, configure real `codex_cli` or
`claude_code_cli` runners and run the full planning, execution, validation,
objective, dashboard, and final-verification path.

## Validation Commands

Common release checks from the repository root:

```bash
python3 scripts/check_package_tree.py
python3 -m unittest tests.test_e2e_smoke tests.test_read_models tests.test_health tests.test_final_verifier
uv run --with jsonschema python -m unittest discover -s tests
git diff --check
```

