from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.adapters.base import AdapterInput
from runtime.adapters.codex_cli_adapter import CodexCliAdapter
from runtime.adapters.codex_executable_resolver import resolve_codex_executable
from runtime.agent_runners import RunnerConfig


def _make_executable(path: Path, body: str = "print('ok')") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)
    return path


def _adapter_input(root: Path, *, command: str, home: Path) -> AdapterInput:
    prompt = root / "prompt.md"
    prompt.write_text("resolver prompt\n", encoding="utf-8")
    config = RunnerConfig(
        runner_id="codex_cli_worker",
        role="worker",
        adapter="codex_cli",
        command=command,
        cwd=root.as_posix(),
        prompt_delivery={"mode": "stdin"},
        args=(),
        env={
            "HOME": home.as_posix(),
            "PATH": "/usr/bin:/bin",
            "LOOPPLANE_CODEX_BIN": "",
            "VSCODE_AGENT_FOLDER": "",
        },
        timeout_seconds=10,
        stream_logs=True,
        permission_policy={
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": True,
            "read_only": False,
        },
        doctor={
            "check_command": f"{command} --version",
            "requires_auth": True,
            "auth_check_command": f"{command} login status",
        },
        enabled=True,
    )
    return AdapterInput.from_runner_config(
        run_id="run_resolver",
        workflow_id="wf_resolver",
        runner_config=config,
        prompt_path=prompt,
        prompt_content="resolver prompt\n",
        scheduler_run_dir=root / "runtime" / "run_resolver",
        role_output_dir=root / "results" / "run_resolver",
        task_id="T001",
        task_evidence_run_dir=root / "results" / "run_resolver",
        cwd=root.as_posix(),
    )


class CodexExecutableResolverTest(unittest.TestCase):
    def test_explicit_override_wins_and_existing_configured_command_wins_without_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = _make_executable(root / "configured" / "codex")
            override = _make_executable(root / "override" / "codex")

            normal = resolve_codex_executable(
                configured.as_posix(),
                env={"PATH": ""},
                cwd=root,
                extension_roots=(),
            )
            selected = resolve_codex_executable(
                configured.as_posix(),
                env={"PATH": "", "LOOPPLANE_CODEX_BIN": override.as_posix()},
                cwd=root,
                extension_roots=(),
            )

            self.assertEqual(normal.resolved_path, configured.resolve().as_posix())
            self.assertEqual(normal.source, "configured_path")
            self.assertFalse(normal.recovered)
            self.assertEqual(selected.resolved_path, override.resolve().as_posix())
            self.assertEqual(selected.source, "environment_override")
            self.assertFalse(selected.recovered)

    def test_newest_editor_extension_recovers_stale_codex_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension_root = root / "extensions"
            older = _make_executable(
                extension_root / "openai.chatgpt-1.0.0-linux-x64" / "bin" / "linux-x86_64" / "codex"
            )
            newer = _make_executable(
                extension_root / "openai.chatgpt-2.0.0-linux-x64" / "bin" / "linux-x86_64" / "codex"
            )
            os.utime(older, ns=(2_000_000_000, 2_000_000_000))
            os.utime(newer, ns=(1_000_000_000, 1_000_000_000))

            resolution = resolve_codex_executable(
                (extension_root / "openai.chatgpt-removed" / "bin" / "linux-x86_64" / "codex").as_posix(),
                env={"PATH": "", "HOME": (root / "unrelated-home").as_posix()},
                cwd=root,
            )

            self.assertEqual(resolution.resolved_path, newer.resolve().as_posix())
            self.assertEqual(resolution.invocation_program, newer.resolve().as_posix())
            self.assertEqual(resolution.source, "vscode_extension")
            self.assertTrue(resolution.recovered)

    def test_missing_arbitrary_program_is_not_replaced_by_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extension_root = root / "extensions"
            _make_executable(
                extension_root / "openai.chatgpt-2.0.0-linux-x64" / "bin" / "linux-x86_64" / "codex"
            )

            resolution = resolve_codex_executable(
                (root / "missing-custom-runner").as_posix(),
                env={"PATH": ""},
                cwd=root,
                extension_roots=(extension_root,),
            )

            self.assertFalse(resolution.available)
            self.assertEqual(resolution.source, "unresolved")
            self.assertFalse(resolution.recovered)

    def test_doctor_and_execution_share_recovery_for_stale_extension_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            current = _make_executable(
                home
                / ".vscode-server"
                / "extensions"
                / "openai.chatgpt-2.0.0-linux-x64"
                / "bin"
                / "linux-x86_64"
                / "codex",
                body="\n".join(
                    (
                        "import os",
                        "import pathlib",
                        "import sys",
                        "if sys.argv[1:] == ['--version']:",
                        "    print('codex-current 2.0.0')",
                        "    raise SystemExit(0)",
                        "if sys.argv[1:] == ['login', 'status']:",
                        "    print('logged in')",
                        "    raise SystemExit(0)",
                        "prompt = sys.stdin.read()",
                        "pathlib.Path(os.environ['LOOPPLANE_FINAL_OUTPUT']).write_text('CURRENT:' + prompt, encoding='utf-8')",
                        "print('ran-current-codex')",
                    )
                ),
            )
            stale = (
                home
                / ".vscode-server"
                / "extensions"
                / "openai.chatgpt-1.0.0-linux-x64"
                / "bin"
                / "linux-x86_64"
                / "codex"
            )
            current_input = _adapter_input(root, command=stale.as_posix(), home=home)

            doctor = CodexCliAdapter().doctor(current_input)
            result = CodexCliAdapter().run(current_input)

            command_check = next(check for check in doctor.checks if check["name"] == "command_exists")
            version_check = next(check for check in doctor.checks if check["name"] == "version_command")
            auth_check = next(check for check in doctor.checks if check["name"] == "authentication")
            self.assertEqual(doctor.status, "ok")
            self.assertEqual(command_check["status"], "warning")
            self.assertEqual(command_check["code"], "command_recovered")
            self.assertEqual(command_check["resolved_path"], current.resolve().as_posix())
            self.assertEqual(version_check["argv"][0], current.resolve().as_posix())
            self.assertEqual(auth_check["argv"][0], current.resolve().as_posix())
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.command, stale.as_posix())
            self.assertEqual(result.adapter_metadata["argv"][0], current.resolve().as_posix())
            self.assertEqual(
                result.adapter_metadata["codex_executable_resolution"],
                {
                    "configured_program": stale.as_posix(),
                    "invocation_program": current.resolve().as_posix(),
                    "resolved_path": current.resolve().as_posix(),
                    "source": "vscode_extension",
                    "recovered": True,
                },
            )
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "CURRENT:resolver prompt\n")
            saved_input = json.loads(
                (root / "runtime" / "run_resolver" / "adapter_input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved_input["command"], stale.as_posix())


if __name__ == "__main__":
    unittest.main()
