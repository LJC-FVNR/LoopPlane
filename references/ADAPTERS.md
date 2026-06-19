# LoopPlane Adapter Reference

Source anchors: `LoopPlane.md` sections 7.1-7.2, 9.9, 10.5, 11, 14.3-14.4, and
23.1; especially lines 547-603, 1147-1231, 2479-2489, 2607-2695, 3013-3094,
and 3830-3843.

`LoopPlane.md` remains authoritative. This document summarizes the external agent
runner adapter contract.

## Purpose

LoopPlane is not bound to one model provider or CLI flag set. Agent runner adapters
encapsulate provider-specific CLI behavior while preserving stable protocol
inputs, outputs, paths, permissions, and role boundaries.

The package layout includes a base contract, command policy module, noop
adapter, shell adapter, Codex CLI adapter, Claude Code CLI adapter, and built-in
adapter registry. Later implementations may add adapters without changing the
protocol authority model.

## Runner Configuration

`agent_runners.json` configures external CLI agents. Runner records include the
adapter, role, enabled state, command, working directory, prompt delivery mode,
args, env, timeout, streaming behavior, permission policy, and doctor checks.
CLI-specific flags belong in adapters and should not become protocol
requirements.
Runner records may also include an optional `resource_policy` for shared runner
resources. When present after inheritance and local overrides, it must include
`global_concurrency_limit`, `lock_scope`, `lock_key`, and `queue_when_busy`.
Supported `lock_scope` values are `machine` and `workspace`; machine-scoped
runtime locks are reserved for `$LOOPPLANE_HOME/locks/runner_locks/<lock_key>.lock`.

Planner, auditor, worker, inspector, and other role runners may inherit common
configuration, but the resulting role and permission policy must still match
the role boundary in the protocol.

The runtime loader resolves inheritance before adapters receive a runner
record. Child scalar and list values replace parent values, while object values
such as `env`, `prompt_delivery`, `permission_policy`, `doctor`, and
`resource_policy` are merged recursively with child keys taking precedence.
Missing parents, inheritance cycles, unknown fields, unsupported prompt
delivery or resource-policy values, and missing required resolved fields are
configuration errors.

## Adapter Input

Adapter input includes schema version, run ID, workflow ID, runner ID, role,
optional task ID, prompt path, scheduler run directory, role output directory,
task evidence run directory when applicable, working directory, command, args,
environment, timeout, and permission policy.

For non-task roles, `task_id` and `task_evidence_run_dir` may be null.

The runtime base contract additionally carries the resolved runner config,
adapter name, prompt delivery config, and prompt content so concrete adapters
can operate without re-reading global configuration. `AdapterInput` can be
created from a resolved `RunnerConfig`, serialized to JSON, and loaded back
without losing path, environment, timeout, or permission fields.

## Adapter Output

Adapter output records schema version, run ID, runner ID, role, adapter,
command, working directory, timestamps, exit code, timeout flag, stdout path,
stderr path, final output path, produced files, and adapter metadata.

Adapters write `adapter_input.json`, stdout, stderr, final output, and
`adapter_result.json`. They do not decide task completion, modify `PLAN.md`, or
mutate runtime state.
The base contract exposes default run-local paths for `stdout.log`,
`stderr.log`, `final.md`, and `adapter_result.json`; serialized output also
records `adapter_result_path` so downstream readers can locate the durable
result file directly. `produced_files` is populated from files created in the
scheduler run directory, role output directory, and task evidence run directory,
plus the standard adapter contract artifacts.

For worker and recovery roles, shell-family adapters also snapshot the
configured repository area outside the workspace boundary around external
execution. If a process creates, modifies, or deletes a sibling-project file,
the adapter records the observed paths in
`adapter_metadata.workspace_boundary_policy`, adds the paths to
`produced_files`, and fails the adapter result with the policy-blocked exit
code unless `.loopplane/workspace.json`, `security.json`, and the active task in
`PLAN.md` explicitly allow that out-of-boundary path.

## Prompt Delivery

Supported prompt delivery modes are `file_argument`, `stdin`,
`stdin_or_prompt_flag`, `interactive_terminal`, and `custom_adapter`.

The protocol does not require a specific Codex CLI or Claude Code CLI flag.
Adapters translate the configured prompt delivery into the target CLI's
supported invocation.

Built-in shell-family adapters (`shell`, `codex_cli`, and `claude_code_cli`)
support the modes that can be represented by captured subprocess I/O:

| Mode | Built-in behavior |
| --- | --- |
| `stdin` | Send `prompt_content` to process stdin. |
| `file_argument` | Append `argument_template` after template expansion, or append `prompt_path` when no template is configured. |
| `stdin_or_prompt_flag` | Use `prompt_flag` plus `prompt_file` when both are configured, use `prompt_file` alone when only that is configured, otherwise fall back to stdin. |
| `interactive_terminal` | Not representable by the captured subprocess contract. Built-in doctors report `waiting_config` and the runner must be reconfigured or handled by a registered terminal-capable adapter. |
| `custom_adapter` | Extension path only. Built-in shell-family doctors report `waiting_config`; a custom `AgentAdapter` implementation must own the delivery semantics. |

Custom adapters should subclass `runtime.adapters.base.AgentAdapter` and be
registered with `runtime.adapters.registry.register_adapter("adapter_name",
AdapterClass)` before scheduler, planner, auditor, or doctor code resolves the
runner. Project configuration may then use that adapter name and
`prompt_delivery.mode="custom_adapter"` when the adapter implements custom
prompt handling. Unregistered adapter names remain configuration problems and
surface as `waiting_config` through runner resolution or doctor checks.

## Doctor Checks

Runner doctor checks should verify that the command exists, a version command
succeeds, authentication is available if required, the working directory is
valid, the permission policy is representable by the adapter, and required
output directories are writable.

Doctor failures move the workflow to `waiting_config`; they are not business
task failures.

Project installation also treats doctor readiness as an install gate for
required external CLI runners. `loopplane skill install --target <project>` and
`loopplane skill update --target <project>` may materialize or attach project files
while still returning `*_waiting_config` when Codex or Claude Code runners
cannot be found, authenticated, or configured. Setup agents must resolve that
runner readiness before invoking planning or scheduler commands.

The adapter doctors inspect availability and configuration without executing
implementation tasks. The noop doctor is always available because it does not
run external commands. The shell doctor checks prompt delivery mode, cwd,
command availability through PATH lookup, scheduler/role/evidence output
directory writability, the configured version command, authentication probes,
and permission policy. The Codex CLI and Claude Code CLI doctors perform the
same non-task checks and report missing commands, version-command failures,
authentication failures, unrepresentable prompt delivery, unwritable output
directories, or permission-policy mismatches as `waiting_config`.

Each doctor check includes a stable `code` field in JSON output, and the
human-readable `loopplane doctor-agent` output prints that code beside the check
name. Important failure codes include:

| Code | Meaning |
| --- | --- |
| `command_missing` | The configured command program cannot be found through PATH or as an executable path. |
| `version_command_failed` | `doctor.check_command` failed, timed out, or could not be executed. |
| `authentication_unavailable` | A configured authentication probe failed or required auth environment variables are missing. |
| `unsupported_prompt_delivery` | The configured prompt delivery mode is not representable by the built-in subprocess adapter. |
| `output_directory_unwritable` | A required scheduler, role, or task evidence output directory could not be created and written. |
| `policy_mismatch` | The configured command and prompt-delivery invocation cannot be represented by the runner permission policy. |

Authentication checks are configuration-driven because the protocol does not
standardize provider-specific login commands. If `doctor.requires_auth=true`,
the doctor can verify authentication through `doctor.auth_check_command` (or the
legacy alias `doctor.check_auth_command`) and/or required `doctor.auth_env_vars`.
When a runner requires auth but neither an auth command nor auth environment
variables are configured, the check reports `authentication_check_not_configured`
without failing the runner; projects that need strict login verification should
set one of those auth probes. `loopplane configure-agent` automatically sets
`doctor.auth_check_command` to `codex login status` for `codex_cli` runners
whose command program is `codex`, and to `claude auth status` for
`claude_code_cli` runners whose command program is `claude`.

`loopplane configure-agent` inspects the effective runner configuration without
mutation when no `--role`, `--adapter`, and `--command` triple is provided.
Supplying that triple writes one existing runner's machine-local command,
doctor probes, and enablement to `$LOOPPLANE_HOME/runners/agent_runners.local.json`;
portable project-local `agent_runners.json` remains unchanged. Project-specific
`.loopplane/config/local/agent_runners.local.json` overrides are also honored and
take precedence over `LOOPPLANE_HOME`. Configuration rejects unknown runners,
unknown adapters, partial mutation arguments, invalid runner JSON, and local
overrides that try to define runner IDs absent from portable config.

During skill installation, LoopPlane performs safe PATH discovery for the default
required Codex-backed runners. When `codex` is found, the installer writes its
absolute path to `.loopplane/config/local/agent_runners.local.json` and immediately
doctors the required runners. When the CLI is missing or authentication fails,
installation remains in `runner_readiness: waiting_config`; an installing agent
should run `command -v codex` or `command -v claude`, configure the discovered
absolute path, and rerun `doctor-agent` before continuing.

`loopplane doctor-agent` doctors the default runner, one named runner with
`--runner`, or every configured runner with `--all`. It returns JSON or
human-readable output and exits nonzero when any selected runner is unknown,
uses an unregistered adapter, has invalid configuration, or reports
`waiting_config`.

## Built-In Adapters

`noop` writes the base contract output files without invoking a subprocess. It
records `external_execution=false` in adapter metadata and is suitable for smoke
validation.

`shell` executes the configured command after permission-policy preflight. It
supports `stdin`, `file_argument`, and `stdin_or_prompt_flag` prompt delivery,
captures stdout and stderr, exposes run paths through `LOOPPLANE_*` environment
variables, preserves a process-written final output file, and writes
`adapter_input.json` and `adapter_result.json` with exit code, timeout,
produced-file, command policy, and workspace-boundary policy metadata.

`noop` and `shell` are deterministic local smoke and integration fixtures. They
do not by themselves satisfy the full `LoopPlane.md` requirement for at least one
CLI agent adapter implementing the runner contract; provider-backed execution
uses `codex_cli` or `claude_code_cli` with installed and authenticated CLIs.

`codex_cli` and `claude_code_cli` execute configured CLI tasks through the same
process contract as the shell adapter. `codex_cli` keeps the stable configured
command as `codex`, but runs it through non-interactive `codex exec`, passes the
LoopPlane prompt on stdin, uses `--ask-for-approval never`, and selects a Codex
sandbox from the runner policy. Non-read-only roles default to
`danger-full-access` so unattended workers can complete end-to-end workflow
steps without sandbox escalation round-trips; read-only runners keep
`read-only` unless configuration overrides it. Both adapters support `stdin`,
`file_argument`, and
`stdin_or_prompt_flag` where those modes can be represented by the runner
configuration, and they record the same stdout, stderr, final output, timeout,
exit-code, policy-decision, produced-file, `adapter_input.json`, and
`adapter_result.json` artifacts. They intentionally report
`interactive_terminal` and `custom_adapter` as `waiting_config` unless a
separate registered adapter owns those semantics.

## Fixture Coverage

The built-in CLI adapter contract is covered by fake `codex` and `claude`
executables under `tests/fixtures/cli_adapters/bin/`. The fixture tests resolve
those commands through `PATH` and exercise `codex_cli` and `claude_code_cli`
without relying on an installed provider CLI or network authentication.
This is fixture coverage, not user-facing provider integrations.

Fixture coverage includes `stdin`, `file_argument`, and
`stdin_or_prompt_flag` prompt delivery, doctor command discovery, stdout and
stderr capture, final output preservation, produced-file discovery,
`adapter_input.json`, `adapter_result.json`, nonzero exits, and timeouts.
Additional smoke tests run `loopplane plan` and `loopplane run` through the fake Codex
CLI adapter to prove planner and worker paths use non-noop CLI execution rather
than falling back to `noop` or `waiting_config`.

## Runtime Integration

The scheduler invokes adapters only after `prepare_run()` creates run
directories, paths, and leases. During worker or recovery execution, the
runtime must capture pre-run and post-run Git metadata outside the adapter's
completion decision. The adapter returns process facts; validation and
reconciliation decide workflow effects.

## Command Policy Enforcement

Adapters and any adapter-owned command-execution bridge must call
`runtime.adapters.policy.enforce_command_policy` before running a command on
behalf of a role. For worker and recovery roles, the policy allows read-only
Git inspection such as `git status` and `git diff`, but blocks write-oriented
Git operations including `commit`, `reset`, `clean`, `checkout`, `switch`,
`branch -D`, `rebase`, `push`, `update-ref`, `tag`, `stash`, and `gc`.

`loopplane vc checkpoint` and `loopplane vc rollback` are Version Control
Manager-only operations. Worker and recovery adapters must block them rather
than invoking the LoopPlane CLI directly.
