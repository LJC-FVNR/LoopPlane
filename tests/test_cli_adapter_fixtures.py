from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.base import AdapterInput, AdapterOutput
from runtime.adapters.claude_code_cli_adapter import ClaudeCodeCliAdapter
from runtime.adapters.codex_cli_adapter import CodexCliAdapter
from runtime.adapters.runner_availability import _cooldown_seconds_from_retry_at, _match_builtin_classifier
from runtime.adapters.shell_adapter import ShellAdapter
from runtime.agent_runners import RunnerConfig
from runtime.exit_codes import ADAPTER_POLICY_BLOCKED_EXIT_CODE, ADAPTER_TIMEOUT_EXIT_CODE


FIXTURE_BIN = Path(__file__).resolve().parent / "fixtures" / "cli_adapters" / "bin"


def _install_fixture_bin(root: Path) -> Path:
    bin_dir = root / "fixture-bin"
    bin_dir.mkdir()
    for name in ("codex", "claude"):
        target = bin_dir / name
        shutil.copy2(FIXTURE_BIN / name, target)
        target.chmod(target.stat().st_mode | 0o111)
    return bin_dir


def _runner_config(
    *,
    adapter: str,
    command: str,
    fixture_bin: Path,
    args: tuple[str, ...] = (),
    prompt_delivery: dict[str, object] | None = None,
    adapter_options: dict[str, object] | None = None,
    timeout_seconds: int = 10,
) -> RunnerConfig:
    return RunnerConfig(
        runner_id=f"{adapter}_worker",
        role="worker",
        adapter=adapter,
        command=command,
        cwd=".",
        prompt_delivery=prompt_delivery or {"mode": "stdin"},
        adapter_options=adapter_options or {},
        args=args,
        env={"PATH": fixture_bin.as_posix() + os.pathsep + os.environ.get("PATH", "")},
        timeout_seconds=timeout_seconds,
        stream_logs=True,
        permission_policy={
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": True,
            "read_only": False,
        },
        doctor={"check_command": f"{command} --version", "requires_auth": False},
        enabled=True,
    )


def _adapter_input(
    root: Path,
    config: RunnerConfig,
    *,
    prompt_content: str,
    run_id: str = "run_fixture",
) -> AdapterInput:
    prompt_path = root / "prompt.md"
    prompt_path.write_text(prompt_content, encoding="utf-8")
    return AdapterInput.from_runner_config(
        run_id=run_id,
        workflow_id="wf_fixture",
        runner_config=config,
        prompt_path=prompt_path,
        prompt_content=prompt_content,
        scheduler_run_dir=root / ".loopplane" / "runtime" / "runs" / run_id,
        role_output_dir=root / ".loopplane" / "results" / "T001" / "runs" / run_id,
        task_id="T001",
        task_evidence_run_dir=root / ".loopplane" / "results" / "T001" / "runs" / run_id,
        cwd=str(root),
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _produced_paths(result: AdapterOutput) -> set[str]:
    return {path.as_posix() for path in result.produced_files}


class CliAdapterFixtureIntegrationTest(unittest.TestCase):
    def test_codex_cli_uses_path_resolved_fixture_and_file_argument_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                prompt_delivery={"mode": "file_argument", "argument_template": "{{prompt_path}}"},
            )
            current_input = _adapter_input(
                root,
                config,
                prompt_content="Run through the fake Codex fixture.\n",
            )

            doctor = CodexCliAdapter().doctor(current_input)
            result = CodexCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "codex_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(doctor.status, "ok")
            self.assertTrue(
                any(
                    check.get("name") == "command_exists"
                    and check.get("resolved_path") == (fixture_bin / "codex").as_posix()
                    for check in doctor.checks
                )
            )
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.command, "codex")
            self.assertEqual(result.adapter, "codex_cli")
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "CODEX STDOUT run=run_fixture\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CODEX STDERR fixture=codex\n")
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "CODEX FINAL\nsource=stdin\nRun through the fake Codex fixture.\n",
            )
            self.assertEqual(record["executable"], (fixture_bin / "codex").resolve().as_posix())
            self.assertEqual(record["prompt_source"], "stdin")
            self.assertEqual(record["prompt"], "Run through the fake Codex fixture.\n")
            self.assertEqual(record["env"]["LOOPPLANE_ROLE"], "worker")  # type: ignore[index]
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )
            self.assertEqual(result.adapter_metadata["delivery_mode"], "file_argument")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["policy_decision"]["allowed"], True)
            self.assert_adapter_contract(result, current_input, extra_produced={record_path.as_posix()})

    def test_codex_cli_uses_configured_sandbox_adapter_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                adapter_options={"codex_sandbox": "danger-full-access"},
            )
            current_input = _adapter_input(root, config, prompt_content="Run with a configured sandbox.\n")

            result = CodexCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )

    def test_codex_cli_uses_configured_model_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                adapter_options={"model": "gpt-5.1-codex", "reasoning_effort": "xhigh"},
            )
            current_input = _adapter_input(root, config, prompt_content="Run with configured model settings.\n")

            result = CodexCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "codex_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--model",
                    "gpt-5.1-codex",
                    "-c",
                    'model_reasoning_effort="xhigh"',
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )
            self.assertEqual(record["env"]["LOOPPLANE_AGENT_MODEL"], "gpt-5.1-codex")  # type: ignore[index]
            self.assertEqual(record["env"]["LOOPPLANE_AGENT_REASONING_EFFORT"], "xhigh")  # type: ignore[index]

    def test_claude_code_cli_uses_path_resolved_fixture_and_prompt_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                prompt_delivery={
                    "mode": "stdin_or_prompt_flag",
                    "prompt_flag": "--prompt-file",
                    "prompt_file": "{{prompt_path}}",
                },
            )
            current_input = _adapter_input(
                root,
                config,
                prompt_content="Run through the fake Claude fixture.\n",
            )

            doctor = ClaudeCodeCliAdapter().doctor(current_input)
            result = ClaudeCodeCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "claude_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(doctor.status, "ok")
            self.assertTrue(
                any(
                    check.get("name") == "command_exists"
                    and check.get("resolved_path") == (fixture_bin / "claude").as_posix()
                    for check in doctor.checks
                )
            )
            self.assertTrue(
                any(
                    check.get("name") == "version_command"
                    and check.get("status") == "ok"
                    and check.get("code") == "version_command_ok"
                    for check in doctor.checks
                )
            )
            self.assertTrue(
                any(
                    check.get("name") == "authentication"
                    and check.get("status") == "ok"
                    and check.get("code") == "authentication_not_required"
                    for check in doctor.checks
                )
            )
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.command, "claude")
            self.assertEqual(result.adapter, "claude_code_cli")
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "CLAUDE STDOUT run=run_fixture\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CLAUDE STDERR fixture=claude\n")
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "CLAUDE FINAL\nsource=file\nRun through the fake Claude fixture.\n",
            )
            self.assertEqual(record["executable"], (fixture_bin / "claude").resolve().as_posix())
            self.assertEqual(
                record["argv"],
                [
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    "--prompt-file",
                    current_input.prompt_path.as_posix(),
                ],
            )
            self.assertEqual(record["prompt_source"], "file")
            self.assertEqual(record["prompt"], "Run through the fake Claude fixture.\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "claude",
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    "--prompt-file",
                    current_input.prompt_path.as_posix(),
                ],
            )
            self.assertEqual(result.adapter_metadata["delivery_mode"], "stdin_or_prompt_flag")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input, extra_produced={record_path.as_posix()})

    def test_claude_code_cli_uses_configured_model_without_forcing_unknown_effort_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                prompt_delivery={
                    "mode": "stdin_or_prompt_flag",
                    "prompt_flag": "--prompt-file",
                    "prompt_file": "{{prompt_path}}",
                },
                adapter_options={"model": "claude-opus-test", "reasoning_effort": "high"},
            )
            current_input = _adapter_input(root, config, prompt_content="Run Claude with configured model settings.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "claude_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "claude",
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--model",
                    "claude-opus-test",
                    "--dangerously-skip-permissions",
                    "--prompt-file",
                    current_input.prompt_path.as_posix(),
                ],
            )
            self.assertEqual(record["env"]["LOOPPLANE_AGENT_MODEL"], "claude-opus-test")  # type: ignore[index]
            self.assertEqual(record["env"]["LOOPPLANE_AGENT_REASONING_EFFORT"], "high")  # type: ignore[index]

    def test_claude_code_cli_fixture_stdin_mode_captures_prompt_from_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="Prompt delivered to Claude on stdin.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "claude_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "CLAUDE STDOUT run=run_fixture\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CLAUDE STDERR fixture=claude\n")
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "CLAUDE FINAL\nsource=stdin\nPrompt delivered to Claude on stdin.\n",
            )
            self.assertEqual(
                record["argv"],
                ["--print", "--output-format=stream-json", "--verbose", "--dangerously-skip-permissions"],
            )
            self.assertEqual(record["prompt_source"], "stdin")
            self.assertEqual(record["prompt"], "Prompt delivered to Claude on stdin.\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                ["claude", "--print", "--output-format=stream-json", "--verbose", "--dangerously-skip-permissions"],
            )
            self.assertEqual(result.adapter_metadata["delivery_mode"], "stdin")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input, extra_produced={record_path.as_posix()})

    def test_claude_code_cli_fixture_file_argument_mode_passes_prompt_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                prompt_delivery={"mode": "file_argument", "argument_template": "{{prompt_path}}"},
            )
            current_input = _adapter_input(root, config, prompt_content="Prompt delivered to Claude by file.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "claude_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "CLAUDE FINAL\nsource=file\nPrompt delivered to Claude by file.\n",
            )
            self.assertEqual(
                record["argv"],
                [
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    current_input.prompt_path.as_posix(),
                ],
            )
            self.assertEqual(record["prompt_source"], "file")
            self.assertEqual(record["prompt"], "Prompt delivered to Claude by file.\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "claude",
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    current_input.prompt_path.as_posix(),
                ],
            )
            self.assertEqual(result.adapter_metadata["delivery_mode"], "file_argument")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input, extra_produced={record_path.as_posix()})

    def test_codex_cli_fixture_stdin_mode_captures_prompt_from_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="Prompt delivered on stdin.\n")

            result = CodexCliAdapter().run(current_input)

            record_path = root / ".loopplane" / "results" / "T001" / "runs" / "run_fixture" / "codex_fixture_record.json"
            record = _read_json(record_path)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                record["argv"],
                [
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )
            self.assertEqual(record["prompt_source"], "stdin")
            self.assertEqual(record["prompt"], "Prompt delivered on stdin.\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "CODEX FINAL\nsource=stdin\nPrompt delivered on stdin.\n")
            self.assert_adapter_contract(result, current_input, extra_produced={record_path.as_posix()})

    def test_codex_cli_fixture_nonzero_exit_records_failure_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                args=("--fail",),
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="This prompt reaches a failing fake CLI.\n")

            result = CodexCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, 17)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CODEX FAILURE requested\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "CODEX FAILURE requested\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "codex",
                    "--ask-for-approval",
                    "never",
                    "exec",
                    "--fail",
                    "--skip-git-repo-check",
                    "--sandbox",
                    "danger-full-access",
                    "-",
                ],
            )
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input)

    def test_codex_cli_fixture_usage_limit_sets_runner_availability_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="codex_cli",
                command="codex",
                fixture_bin=fixture_bin,
                args=("--usage-limit",),
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="This prompt reaches a usage-limited fake CLI.\n")

            result = CodexCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, 18)
            availability = result.adapter_metadata.get("runner_availability")
            self.assertIsInstance(availability, Mapping)
            assert isinstance(availability, Mapping)
            self.assertEqual(availability["status"], "unavailable")
            self.assertEqual(availability["reason_class"], "usage_limit_exhausted")
            self.assertEqual(availability["recoverability"], "auto_after_cooldown")
            self.assertEqual(availability["scope"], {"type": "runner", "key": "codex_cli_worker"})
            self.assertEqual(availability["retry_after_seconds"], 18000)
            self.assertIn("cooldown_until", availability)
            self.assert_adapter_contract(result, current_input)

    def test_usage_limit_absolute_retry_time_uses_short_cooldown(self) -> None:
        now = datetime(2026, 6, 27, 20, 58, 0, tzinfo=UTC)

        cooldown = _cooldown_seconds_from_retry_at(
            "ERROR: You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
            "to purchase more credits or try again at 9:05 PM.",
            now=now,
        )

        self.assertEqual(cooldown, 480)

    def test_usage_limit_elapsed_absolute_retry_time_does_not_hold_until_next_day(self) -> None:
        now = datetime(2026, 6, 27, 22, 12, 0, tzinfo=UTC)

        cooldown = _cooldown_seconds_from_retry_at(
            "ERROR: You've hit your usage limit. Try again at 9:05 PM.",
            now=now,
        )

        self.assertEqual(cooldown, 60)

    def test_bare_prompt_line_number_402_is_not_billing_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "402- - approval: not_required\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNone(match)

    def test_http_402_remains_billing_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "HTTP 402: payment required\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "billing_required")

    def test_prompt_billing_instruction_is_not_billing_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": (
                    "Account rule: if the Slurm account has insufficient balance, credits, "
                    "or billing quota, retry the paid account.\n"
                ),
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNone(match)

    def test_model_capacity_is_retryable_provider_overload(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "ERROR: Selected model is at capacity. Please try a different model.\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "provider_overloaded")
        self.assertEqual(match["cooldown_seconds"], 300)

    def test_slurm_tres_billing_field_does_not_override_terminal_capacity_error(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": (
                    "ReqTRES=cpu=832,mem=8231000M,node=13,billing=328352,gres/gpu=32\n"
                    "ERROR: Selected model is at capacity. Please try a different model.\n"
                ),
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "provider_overloaded")
        self.assertFalse(match.get("requires_attention", False))

    def test_invalid_cli_argument_is_manual_runner_configuration_error(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "error: unexpected argument '--effort' found\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "runner_configuration_error")
        self.assertTrue(match["requires_attention"])

    def test_terminal_capacity_error_wins_over_earlier_availability_language(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": (
                    "Example source text: HTTP 402 payment required.\n"
                    "worker continued processing\n"
                    "ERROR: Selected model is at capacity. Please try a different model.\n"
                ),
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "provider_overloaded")
        self.assertIn("at capacity", match["message"])

    def test_bare_numeric_status_like_file_sizes_are_not_availability_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "-rwxrwxr-x 1 nvidia nvidia 429 Apr 2 06:51 write_spec.sh\n",
                "stdout": "source lines 401 503 529\n",
                "final_output": "",
            }
        )

        self.assertIsNone(match)

    def test_http_429_remains_rate_limit_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "HTTP status 429: too many requests\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "rate_limited")

    def test_model_capacity_wins_over_prompt_auth_words(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": (
                    "user\n"
                    "Final verification rejects unauthorized skipped work.\n"
                    "ERROR: Selected model is at capacity. Please try a different model.\n"
                ),
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "provider_overloaded")
        self.assertEqual(match["cooldown_seconds"], 300)

    def test_prompt_prose_with_unauthorized_is_not_auth_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "Hidden bytes or unauthorized GPUs invalidate the objective.\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNone(match)

    def test_standalone_unauthorized_error_remains_auth_evidence(self) -> None:
        match = _match_builtin_classifier(
            {
                "stderr": "ERROR: Unauthorized\n",
                "stdout": "",
                "final_output": "",
            }
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["reason_class"], "auth_required")

    def test_shell_adapter_accepts_custom_runner_availability_classifier(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            script = root / "custom_cli.py"
            script.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "import sys",
                        "print('MYCLI TEMPORARY CREDIT WINDOW', file=sys.stderr)",
                        "raise SystemExit(42)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = _runner_config(
                adapter="shell",
                command=sys.executable,
                fixture_bin=fixture_bin,
                args=(script.as_posix(),),
                prompt_delivery={"mode": "stdin"},
                adapter_options={
                    "runner_availability": {
                        "scope": {"type": "credential", "key": "custom-account"},
                        "classifiers": [
                            {
                                "reason_class": "custom_credit_window",
                                "match": {"stderr_regex": "MYCLI TEMPORARY CREDIT WINDOW"},
                                "cooldown_seconds": 60,
                                "confidence": "high",
                            }
                        ],
                    }
                },
            )
            current_input = _adapter_input(root, config, prompt_content="Custom CLI prompt.\n")

            result = ShellAdapter().run(current_input)

            self.assertEqual(result.exit_code, 42)
            availability = result.adapter_metadata.get("runner_availability")
            self.assertIsInstance(availability, Mapping)
            assert isinstance(availability, Mapping)
            self.assertEqual(availability["reason_class"], "custom_credit_window")
            self.assertEqual(availability["scope"], {"type": "credential", "key": "custom-account"})
            self.assertEqual(availability["retry_after_seconds"], 60)
            self.assertEqual(availability["confidence"], "high")
            self.assert_adapter_contract(result, current_input)

    def test_shell_adapter_does_not_apply_cli_builtin_availability_without_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            script = root / "plain_shell_failure.py"
            script.write_text(
                "\n".join(
                    [
                        "from __future__ import annotations",
                        "import sys",
                        "print('usage limit reached in a domain-specific test fixture', file=sys.stderr)",
                        "raise SystemExit(42)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = _runner_config(
                adapter="shell",
                command=sys.executable,
                fixture_bin=fixture_bin,
                args=(script.as_posix(),),
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="Plain shell prompt.\n")

            result = ShellAdapter().run(current_input)

            self.assertEqual(result.exit_code, 42)
            self.assertNotIn("runner_availability", result.adapter_metadata)
            self.assert_adapter_contract(result, current_input)

    def test_claude_code_cli_fixture_nonzero_exit_records_failure_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                args=("--fail",),
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="This prompt reaches a failing fake Claude CLI.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, 19)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CLAUDE FAILURE requested\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "CLAUDE FAILURE requested\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                ["claude", "--print", "--output-format=stream-json", "--verbose", "--dangerously-skip-permissions", "--fail"],
            )
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input)

    def test_claude_code_cli_fixture_timeout_records_timeout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            config = _runner_config(
                adapter="claude_code_cli",
                command="claude",
                fixture_bin=fixture_bin,
                args=("--sleep", "5"),
                prompt_delivery={"mode": "stdin"},
                timeout_seconds=1,
            )
            current_input = _adapter_input(root, config, prompt_content="This prompt times out.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, ADAPTER_TIMEOUT_EXIT_CODE)
            self.assertTrue(result.timed_out)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "")
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "Shell adapter command timed out with no output.\n",
            )
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                ["claude", "--print", "--output-format=stream-json", "--verbose", "--dangerously-skip-permissions", "--sleep", "5"],
            )
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assert_adapter_contract(result, current_input)

    def test_claude_code_cli_fixture_blocks_policy_before_external_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_bin = _install_fixture_bin(root)
            fake_git = fixture_bin / "git"
            execution_marker = root / "executed.txt"
            fake_git.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "from __future__ import annotations",
                        "import pathlib",
                        f"pathlib.Path({execution_marker.as_posix()!r}).write_text('executed', encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_git.chmod(fake_git.stat().st_mode | 0o111)
            config = _runner_config(
                adapter="claude_code_cli",
                command="git",
                fixture_bin=fixture_bin,
                args=("commit", "-m", "blocked"),
                prompt_delivery={"mode": "stdin"},
            )
            current_input = _adapter_input(root, config, prompt_content="Policy-blocked Claude prompt.\n")

            result = ClaudeCodeCliAdapter().run(current_input)

            self.assertEqual(result.exit_code, ADAPTER_POLICY_BLOCKED_EXIT_CODE)
            self.assertFalse(result.timed_out)
            self.assertFalse(execution_marker.exists())
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "")
            self.assertIn("write-oriented", result.stderr_path.read_text(encoding="utf-8"))
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "Command blocked by permission policy.\n")
            self.assertEqual(
                list(result.adapter_metadata["argv"]),
                [
                    "git",
                    "--print",
                    "--output-format=stream-json",
                    "--verbose",
                    "--dangerously-skip-permissions",
                    "commit",
                    "-m",
                    "blocked",
                ],
            )
            self.assertFalse(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["policy_decision"]["decision"], "blocked_git_write")
            self.assert_adapter_contract(result, current_input)

    def assert_adapter_contract(
        self,
        result: AdapterOutput,
        adapter_input: AdapterInput,
        *,
        extra_produced: set[str] | None = None,
    ) -> None:
        paths = adapter_input.output_paths()
        expected_paths = {
            (adapter_input.scheduler_run_dir / "adapter_input.json").as_posix(),
            paths.stdout_path.as_posix(),
            paths.stderr_path.as_posix(),
            paths.final_output_path.as_posix(),
            paths.adapter_result_path.as_posix(),
        }
        if extra_produced:
            expected_paths.update(extra_produced)

        self.assertLessEqual(expected_paths, _produced_paths(result))
        saved_input = AdapterInput.read_json(adapter_input.scheduler_run_dir / "adapter_input.json")
        self.assertEqual(saved_input.to_dict(), adapter_input.to_dict())
        saved_result = AdapterOutput.read_json(paths.adapter_result_path)
        self.assertEqual(saved_result.to_dict(), result.to_dict())
        result_json = _read_json(paths.adapter_result_path)
        self.assertEqual(result_json["exit_code"], result.exit_code)
        self.assertEqual(result_json["timed_out"], result.timed_out)
        self.assertEqual(result_json["stdout_path"], paths.stdout_path.as_posix())
        self.assertEqual(result_json["stderr_path"], paths.stderr_path.as_posix())
        self.assertEqual(result_json["final_output_path"], paths.final_output_path.as_posix())
        self.assertEqual(result_json["adapter_result_path"], paths.adapter_result_path.as_posix())


if __name__ == "__main__":
    unittest.main()
