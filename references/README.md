# LoopPlane References

This directory contains concise protocol reference documents derived from
`LoopPlane.md`. The product specification remains authoritative; these files are
navigation aids for implementers and future workflow agents.

## v1.6 Support Status

The standalone implementation supports the v1.6 MVP surfaces that affect
reference behavior: same-workspace workflow history, dashboard workflow
switching, workspace registry/current workflow pointer files, v1.5 flat
compatibility, canonical workflow-root path resolution, workspace and workflow
CLI groups, LOOPPLANE_HOME discovery/local override authority, runner resource
locks, monorepo and nested workspace boundaries, migration export/import
profiles, Git-ref bundle export/import, archived/read-only workflow mutation
safeguards, and release gates for those requirements.

Reference docs should describe `$LOOPPLANE_HOME` as discovery/local machine state,
not workflow authority. They should describe dashboard workflow selection as
visualization by default, not an implicit current-pointer switch. They should
describe migration and Git-ref import/export as stale-state-excluding and
history-preserving, not as process-state transfer or branch rewriting.

Intentionally deferred v1.6 scope remains the `LoopPlane.md` 26.2 MVP deferrals:
multiple concurrently running workflows in one workspace, global
cross-workspace dashboard discovery, parallel workers, cloud or orchestration
backends, advanced graph editing, full semantic LLM validation, complex cost
accounting, multi-user dashboard authentication, and remote browser
collaboration.

## Index

- [PROTOCOL.md](PROTOCOL.md) - authority layers, invariants, workflow shape,
  evidence, and completion rules.
- [RUNTIME_SPEC.md](RUNTIME_SPEC.md) - scheduler, run directories, validation,
  reconciliation, health, preview, and final verification.
- [PLANNER_SPEC.md](PLANNER_SPEC.md) - brief-to-plan initialization, plan draft
  requirements, readiness, audit, and activation boundaries.
- [DASHBOARD_SPEC.md](DASHBOARD_SPEC.md) - read models, dashboard layout,
  request-writing API shape, inspector boundary, and freshness expectations.
- [ADAPTERS.md](ADAPTERS.md) - external CLI runner adapter input, output,
  prompt delivery, doctor checks, and role boundaries.
- [SECURITY.md](SECURITY.md) - permission matrix, protected paths, redaction,
  dashboard security, prompt injection defense, approvals, and Git boundaries.
