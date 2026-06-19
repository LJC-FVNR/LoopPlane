---
name: loopplane
description: Initialize and run a durable, plan-driven agent workflow inside any project using external CLI coding agents.
---

# LoopPlane Skill

Use this skill when the user wants to turn a natural-language objective into a persistent, auditable, recoverable workflow.

Read `references/PROTOCOL.md` first.

Required install-time CLI bootstrap:
- `loopplane skill install --target <project>` is not complete until the required external CLI runners are found, configured, and doctored.
- Before planning, starting, or resuming a provider-backed workflow, find the local Codex CLI path with `command -v codex` when Codex runners are used, and find the Claude Code CLI path with `command -v claude` when Claude runners are used.
- Configure discovered paths into the project environment with `loopplane configure-agent --project <project> --command <absolute-cli-path>` for the relevant runner(s), then run `loopplane doctor-agent --project <project> --runner <runner_id>` or `loopplane doctor-agent --project <project> --all`.
- If `skill install`, `skill update`, or `doctor-agent` reports `*_waiting_config` or `runner_readiness: waiting_config`, do not continue to `plan`, `activate-plan`, `start`, or `resume`; resolve CLI discovery, authentication, or runner configuration first.
- Do not ask the user to manually locate Codex or Claude until you have tried safe PATH discovery and the doctor output still cannot identify a usable installed/authenticated CLI.

Common operations:
- initialize a local workflow instance;
- turn a user brief into an audited `PLAN.md`;
- configure CLI agent runners;
- start, pause, resume, or inspect the workflow;
- open the dashboard;
- create change requests.
