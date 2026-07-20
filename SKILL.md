---
name: loopplane
description: Initialize and run a durable, plan-driven agent workflow inside any project using external CLI coding agents.
---

# LoopPlane Skill

Use this skill when the user wants to turn a natural-language objective into a persistent, auditable, recoverable workflow.

LoopPlane owns agent workflow orchestration, not the project's execution
backend. Keep machine, resource, launcher, and storage instructions in project
shared context, runner configuration, or optional project skills. Never add
site-specific command interception or resource policy to the generic runtime.

Read `references/PROTOCOL.md` first when installing, operating, debugging, or
changing LoopPlane itself. Generated worker, recovery, validator, inspector,
and summary runs already receive their role contract in the prompt; they must
not reread the full protocol unless a concrete lifecycle or authority ambiguity
requires it.

Required install-time CLI bootstrap:
- `loopplane skill install --target <project>` is not complete until the required external CLI runners are found, configured, and doctored.
- Before planning, starting, or resuming a provider-backed workflow, configure Codex runners with the stable command `codex`; LoopPlane resolves the current PATH or editor-extension binary at doctor and execution time. Find the Claude Code CLI path with `command -v claude` when Claude runners are used.
- Configure the relevant runner(s) with `loopplane configure-agent --project <project> --command codex` for Codex or the discovered Claude command, then run `loopplane doctor-agent --project <project> --runner <runner_id>` or `loopplane doctor-agent --project <project> --all`.
- If `skill install`, `skill update`, or `doctor-agent` reports `*_waiting_config` or `runner_readiness: waiting_config`, do not continue to `plan`, `activate-plan`, `start`, or `resume`; resolve CLI discovery, authentication, or runner configuration first.
- Do not ask the user to manually locate Codex or Claude until you have tried safe PATH discovery and the doctor output still cannot identify a usable installed/authenticated CLI.

Common operations:
- initialize a local workflow instance;
- turn a user brief into an audited `PLAN.md`;
- configure CLI agent runners;
- start, pause, resume, or inspect the workflow;
- open the dashboard;
- create change requests.
