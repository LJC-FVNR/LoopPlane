# LoopPlane Runtime Adapters

`base.py` contains the shared adapter contract used by concrete agent runner
adapters. It defines `AdapterInput`, `AdapterOutput`, `AdapterOutputPaths`,
`AdapterDoctorResult`, and `AgentAdapter`, plus helpers for run directories,
default input/stdout/stderr/final/result paths, JSON round-trips, timestamps,
writing `adapter_input.json` and `adapter_result.json`, and discovering files
created in run/evidence directories.

`policy.py` contains the shared command classifier and enforcement hook that
adapters must call before running commands for a workflow role. It blocks
worker and recovery attempts to run write-oriented Git commands and limits
`loopplane vc checkpoint` / `loopplane vc rollback` to the Version Control Manager.

`boundary.py` contains shell-family adapter path policy checks for worker and
recovery roles. It snapshots the enclosing repository area outside the
configured workspace boundary before and after external execution, records
observed sibling-project changes in `adapter_metadata.workspace_boundary_policy`,
and turns unauthorized out-of-boundary edits into a policy-blocked adapter
result.

`noop_adapter.py` implements a no-external-execution adapter that writes the
standard `adapter_input.json`, stdout, stderr, final output, and
`adapter_result.json` files. It is intended for smoke tests and
disabled/default-safe workflows.

`shell_adapter.py` implements a local shell/process adapter. It builds argv from
the resolved runner config, supports `stdin`, `file_argument`, and
`stdin_or_prompt_flag` prompt delivery, injects run path environment variables,
enforces command policy before execution, captures stdout/stderr, preserves a
process-written final output file, and records process facts in
`adapter_result.json`. `stdin_or_prompt_flag` uses `prompt_flag` plus
`prompt_file` when both are configured, `prompt_file` alone when only that is
configured, and stdin otherwise. Produced files include the standard adapter
artifacts and files discovered under the role output or task evidence run
directory, plus any observed sibling-project paths from the workspace-boundary
policy check.

`noop` and `shell` are deterministic local smoke and integration fixtures. The
shell adapter is useful for reproducible tests, migration checks, scripted
maintenance, and narrow local harnesses, but it should not be treated as the
default durable-loop intelligence layer. For open-ended project work, configure
`codex_cli` or `claude_code_cli` once; planner, auditor, validator,
objective-verifier, expansion, summary, reviewer, and inspector runners inherit
that base CLI runner unless intentionally overridden.

`codex_cli_adapter.py` and `claude_code_cli_adapter.py` are CLI adapter
specializations over the shell process contract. They execute configured CLI
tasks through the standard prompt-delivery, command-policy, log-capture, final
output, timeout, and `adapter_result.json` path, while their doctor checks
inspect command availability, configured version commands, configurable
authentication probes, cwd, prompt delivery, output-directory writability, and
permission policy without executing implementation prompts. Doctor JSON checks
carry stable `code` values such as `command_missing`,
`version_command_failed`, `authentication_unavailable`,
`unsupported_prompt_delivery`, `output_directory_unwritable`, and
`policy_mismatch`; `loopplane doctor-agent` prints those codes in text output too.

The `claude_code_cli` adapter injects `--print --output-format=stream-json
--verbose` so the Claude CLI emits one JSON event per line as work happens
(plain `--print` text output only flushes at the end of the turn, leaving
`stdout.log` empty until completion). Raw stream-json is unreadable and large —
echoed tool inputs and results are often >70% of its bytes — so the adapter
pipes stdout through `ClaudeStreamRenderer`, which rewrites each event to a
compact line as it streams: assistant text verbatim, tool calls as `🔧
Name(arg)`, tool results as `↳ result (Nb): <preview>` (size plus a clipped
preview), and a closing `✓ done · Ns · $cost` marker; token-streaming chunks are
dropped. This keeps the dashboard's live log tail current while shrinking
`stdout.log` roughly 25–35x, and the renderer captures the final answer for
`final.md`. Lines that are not JSON (text-output mode, an error banner) pass
through verbatim. Tuning knobs in `adapter_options`: set `claude_stream_logs:
false` to revert to buffered plain-text output (no streaming, no renderer);
`claude_log_result_preview_chars` to change the tool-result preview length (set
`0` to keep only the size marker). Supplying your own `--output-format` also
disables the injection and renderer, leaving your chosen format untouched.

Authentication probes are configured under the runner's `doctor` object with
`auth_check_command` or `auth_env_vars` because provider-specific login checks
are intentionally outside the stable adapter protocol. `check_auth_command` is
accepted as a compatibility alias for `auth_check_command`. The
`configure-agent` command sets `codex login status` as the auth probe when a
`codex_cli` runner uses the real `codex` command and stores that probe in the
machine-local runner override file, not portable workflow truth.

The Codex specialization preserves the documented runner command as `codex`
while invoking the process as `codex exec` for non-interactive runs. It sends
the LoopPlane prompt on stdin, adds `--ask-for-approval never` and
`--skip-git-repo-check`, and chooses a Codex sandbox from the runner permission
policy. Non-read-only roles default to `danger-full-access`; read-only runners
keep `read-only` unless configuration overrides it.

`interactive_terminal` is not representable by these captured subprocess
adapters. Their doctor checks report `waiting_config` for that mode until the
runner is reconfigured or a terminal-capable adapter is registered.

`custom_adapter` is the extension path. A custom adapter should subclass
`runtime.adapters.base.AgentAdapter`, implement `run()` and `doctor()`, and be
registered with `runtime.adapters.registry.register_adapter()` before runtime
code resolves the configured runner. Built-in shell-family adapters report
`waiting_config` for `custom_adapter` because custom delivery semantics must be
owned by the custom adapter itself.

`tests/fixtures/cli_adapters/bin/` contains fake `codex` and `claude`
executables used by adapter integration tests. Those tests verify command
resolution through `PATH`, `stdin`, `file_argument`, and
`stdin_or_prompt_flag` prompt delivery, stdout/stderr capture, final output,
produced-file discovery, `adapter_input.json`, `adapter_result.json`, nonzero
exit recording, and timeout recording for the concrete CLI adapters. This is
fixture coverage, not user-facing provider integrations. Planner and worker
smoke tests also use the fake Codex executable to exercise `loopplane plan` and
`loopplane run` through `codex_cli` without falling back to `noop` or
`waiting_config`.

`registry.py` maps built-in adapter names such as `noop`, `shell`,
`codex_cli`, and `claude_code_cli` to adapter instances for scheduler and
doctor-agent plumbing. It also exposes `register_adapter()` for project-local
or package-provided adapter extensions.
