from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.agent_runners import agent_runner_project_key
from runtime.init_workflow import init_project


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


class AgentRunnerCliTest(unittest.TestCase):
    def run_loopplane(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LoopPlane), *args],
            cwd=cwd or REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def init_project(self, root: Path) -> Path:
        project = root / "project"
        init_project(project, "Configure agent runners.")
        return project

    def read_agent_config(self, project: Path) -> dict[str, object]:
        config_path = project / ".loopplane" / "config" / "agent_runners.json"
        return json.loads(config_path.read_text(encoding="utf-8"))

    def env_for_home(self, root: Path) -> dict[str, str]:
        return {**os.environ, "LOOPPLANE_HOME": str(root / "loopplane-home")}

    def read_home_local_config(self, home: Path) -> dict[str, object]:
        return json.loads((home / "runners" / "agent_runners.local.json").read_text(encoding="utf-8"))

    def test_configure_agent_inspects_runner_without_mutating_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self.init_project(Path(tmp))
            before = (project / ".loopplane" / "config" / "agent_runners.json").read_text(encoding="utf-8")

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            after = (project / ".loopplane" / "config" / "agent_runners.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)
            self.assertFalse(data["mutated"])
            self.assertEqual(data["selected_runner_ids"], ["worker"])
            self.assertEqual(data["runners"]["worker"]["adapter"], "codex_cli")

    def test_configure_agent_help_mentions_doctor_option_conflict(self) -> None:
        result = self.run_loopplane("configure-agent", "--help")

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        help_text = " ".join(result.stdout.split())
        self.assertIn("mutually exclusive with --no-version-check", help_text)
        self.assertIn("mutually exclusive with --doctor-check-command", help_text)

    def test_configure_agent_no_version_check_allows_empty_doctor_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "shell",
                "--command",
                sys.executable,
                "--no-version-check",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "ok")
            local = self.read_home_local_config(Path(env["LOOPPLANE_HOME"]))
            doctor = local["projects"][agent_runner_project_key(project)]["runners"]["worker"]["doctor"]  # type: ignore[index]
            self.assertEqual(doctor["check_kind"], "none")
            self.assertEqual(doctor["check_command"], "")

    def test_custom_doctor_check_uses_readiness_labels_and_clear_override_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            worker = project / "worker.py"
            worker.write_text("print('worker fixture')\n", encoding="utf-8")
            doctor_command = f"{sys.executable} {worker.as_posix()}"

            configure = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "shell",
                "--command",
                sys.executable,
                "--arg",
                worker.as_posix(),
                "--doctor-check-command",
                doctor_command,
                env=env,
            )
            doctor = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)

            self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)
            self.assertIn("LOOPPLANE_HOME override path:", configure.stdout)
            self.assertIn("Project-local override path:", configure.stdout)
            self.assertIn("Effective write path:", configure.stdout)
            self.assertIn("machine-local, not portable", configure.stdout)
            self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
            data = json.loads(doctor.stdout)
            self.assertFalse(data["override_portability"]["portable"])
            self.assertIn("not portable", data["override_portability"]["note"])
            checks = data["runner_results"][0]["checks"]
            self.assertTrue(any(check.get("name") == "doctor_check" and check.get("code") == "doctor_check_ok" for check in checks))
            self.assertFalse(any(check.get("name") == "version_command" for check in checks))

    def test_configure_agent_inspect_preserves_resolved_resource_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self.init_project(Path(tmp))
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = self.read_agent_config(project)
            config["runners"]["worker"]["resource_policy"] = {  # type: ignore[index]
                "global_concurrency_limit": 1,
                "lock_scope": "machine",
                "lock_key": "codex_cli_default",
                "queue_when_busy": True,
            }
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertFalse(data["mutated"])
            self.assertEqual(
                data["runners"]["worker"]["resource_policy"],
                {
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": "codex_cli_default",
                    "queue_when_busy": True,
                },
            )

    def test_configure_agent_updates_temp_project_runner_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            before = config_path.read_text(encoding="utf-8")

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            config = self.read_agent_config(project)
            worker = config["runners"]["worker"]  # type: ignore[index]
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            local_runner = home_payload["projects"][project_key]["runners"]["worker"]  # type: ignore[index]
            self.assertTrue(data["mutated"])
            self.assertEqual(data["selected_runner_ids"], ["worker"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertEqual(worker["command"], "codex")
            self.assertNotIn(sys.executable, config_path.read_text(encoding="utf-8"))
            self.assertEqual(local_runner["adapter"], "codex_cli")  # type: ignore[index]
            self.assertEqual(local_runner["command"], sys.executable)  # type: ignore[index]
            self.assertTrue(local_runner["enabled"])  # type: ignore[index]
            self.assertEqual(local_runner["doctor"]["check_command"], f"{sys.executable} --version")  # type: ignore[index]
            self.assertEqual(local_runner["doctor"]["auth_check_command"], "")  # type: ignore[index]
            self.assertEqual(data["runners"]["worker"]["command"], sys.executable)
            self.assertTrue((project / ".loopplane" / "config" / "local" / ".gitignore").is_file())

            inspect = self.run_loopplane("configure-agent", "--project", str(project), "--runner", "worker", "--json", env=env)
            self.assertEqual(inspect.returncode, 0, inspect.stderr + inspect.stdout)
            inspect_data = json.loads(inspect.stdout)
            self.assertTrue(inspect_data["local_override_paths"])
            self.assertEqual(inspect_data["local_override_runner_ids"], ["worker"])

    def test_configure_agent_removes_shadowing_project_local_override_when_writing_home_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            project_local = project / ".loopplane" / "config" / "local" / "agent_runners.local.json"
            project_local.parent.mkdir(parents=True, exist_ok=True)
            project_local.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "authority": "machine_local",
                        "runners": {
                            "worker": {
                                "role": "worker",
                                "adapter": "codex_cli",
                                "command": "/tmp/stale-codex",
                                "enabled": True,
                            }
                        },
                        "projects": {},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertTrue(data["local_overrides"]["shadowed_project_local_override_removed"])
            self.assertEqual(data["runners"]["worker"]["command"], sys.executable)
            project_local_payload = json.loads(project_local.read_text(encoding="utf-8"))
            self.assertNotIn("worker", project_local_payload["runners"])

            inspect = self.run_loopplane("configure-agent", "--project", str(project), "--runner", "worker", "--json", env=env)
            self.assertEqual(inspect.returncode, 0, inspect.stderr + inspect.stdout)
            self.assertEqual(json.loads(inspect.stdout)["runners"]["worker"]["command"], sys.executable)

    def test_configure_agent_can_set_codex_sandbox_adapter_option(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--codex-sandbox",
                "danger-full-access",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            runner = home_payload["projects"][project_key]["runners"]["worker"]  # type: ignore[index]
            self.assertEqual(runner["adapter_options"]["codex_sandbox"], "danger-full-access")  # type: ignore[index]
            self.assertEqual(data["runners"]["worker"]["adapter_options"]["codex_sandbox"], "danger-full-access")

    def test_configure_agent_can_set_model_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--model",
                "gpt-5.1-codex",
                "--reasoning-effort",
                "high",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            runner = home_payload["projects"][project_key]["runners"]["worker"]  # type: ignore[index]
            self.assertEqual(runner["adapter_options"]["model"], "gpt-5.1-codex")  # type: ignore[index]
            self.assertEqual(runner["adapter_options"]["reasoning_effort"], "high")  # type: ignore[index]
            self.assertEqual(data["runners"]["worker"]["adapter_options"]["model"], "gpt-5.1-codex")
            self.assertEqual(data["runners"]["worker"]["adapter_options"]["reasoning_effort"], "high")

    def test_partial_configure_agent_preserves_project_local_discovered_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            project_local = project / ".loopplane" / "config" / "local" / "agent_runners.local.json"
            project_local.parent.mkdir(parents=True, exist_ok=True)
            project_local.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "authority": "machine_local",
                        "runners": {
                            "worker": {
                                "role": "worker",
                                "adapter": "codex_cli",
                                "command": sys.executable,
                                "enabled": True,
                            }
                        },
                        "projects": {},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--codex-sandbox",
                "danger-full-access",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data["runners"]["worker"]["command"], sys.executable)
            self.assertEqual(data["local_overrides"]["scope"], "project_local")
            self.assertFalse(data["local_overrides"]["shadowed_project_local_override_removed"])
            project_local_payload = json.loads(project_local.read_text(encoding="utf-8"))
            local_runner = project_local_payload["runners"]["worker"]
            self.assertEqual(local_runner["command"], sys.executable)
            self.assertEqual(local_runner["adapter_options"]["codex_sandbox"], "danger-full-access")

    def test_configure_agent_prunes_stale_loopplane_home_project_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            home = root / "loopplane-home"
            stale_project = root / "deleted-project"
            current_key = agent_runner_project_key(project)
            stale_key = agent_runner_project_key(stale_project)
            local_path = home / "runners" / "agent_runners.local.json"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "authority": "machine_local",
                        "runners": {},
                        "projects": {
                            current_key: {
                                "project_root": project.resolve().as_posix(),
                                "authority": "machine_local",
                                "runners": {"worker": {"command": sys.executable}},
                            },
                            stale_key: {
                                "project_root": stale_project.resolve().as_posix(),
                                "authority": "machine_local",
                                "runners": {"worker": {"command": "/tmp/stale-codex"}},
                            },
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane("configure-agent", "--project", str(project), "--prune", "--json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertTrue(data["mutated"])
            self.assertEqual(data["local_overrides"]["pruned_projects"], 1)
            payload = self.read_home_local_config(home)
            self.assertIn(current_key, payload["projects"])
            self.assertNotIn(stale_key, payload["projects"])

    def test_configure_agent_updates_shell_runner_execution_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "shell",
                "--command",
                sys.executable,
                "--arg",
                "-p",
                "--arg",
                "worker.py",
                "--arg=--flag",
                "--cwd",
                "{{project_root}}/workers",
                "--prompt-delivery-mode",
                "file_argument",
                "--prompt-argument-template=--prompt={prompt_path}",
                "--prompt-flag=--prompt-file",
                "--prompt-file",
                "{{run_dir}}/prompt.md",
                "--timeout-seconds",
                "123",
                "--env",
                "LOOPPLANE_FIXTURE=ok",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            runner = home_payload["projects"][project_key]["runners"]["worker"]  # type: ignore[index]
            self.assertEqual(runner["adapter"], "shell")  # type: ignore[index]
            self.assertEqual(runner["command"], sys.executable)  # type: ignore[index]
            self.assertEqual(runner["args"], ["-p", "worker.py", "--flag"])  # type: ignore[index]
            self.assertEqual(runner["cwd"], "{{project_root}}/workers")  # type: ignore[index]
            self.assertEqual(runner["timeout_seconds"], 123)  # type: ignore[index]
            self.assertEqual(runner["env"]["LOOPPLANE_FIXTURE"], "ok")  # type: ignore[index]
            self.assertEqual(  # type: ignore[index]
                runner["prompt_delivery"],
                {
                    "mode": "file_argument",
                    "argument_template": "--prompt={prompt_path}",
                    "prompt_file": "{{run_dir}}/prompt.md",
                    "prompt_flag": "--prompt-file",
                },
            )
            self.assertEqual(data["runners"]["worker"]["args"], ["-p", "worker.py", "--flag"])

            stdin_result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--prompt-delivery-mode",
                "stdin",
                "--json",
                env=env,
            )

            self.assertEqual(stdin_result.returncode, 0, stdin_result.stderr + stdin_result.stdout)
            stdin_payload = json.loads(stdin_result.stdout)
            self.assertEqual(stdin_payload["runners"]["worker"]["prompt_delivery"], {"mode": "stdin"})

    def test_configure_agent_codex_command_sets_login_status_auth_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "planner",
                "--adapter",
                "codex_cli",
                "--command",
                "codex",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            planner = home_payload["projects"][project_key]["runners"]["planner"]  # type: ignore[index]
            self.assertEqual(planner["doctor"]["check_command"], "codex --version")  # type: ignore[index]
            self.assertEqual(planner["doctor"]["auth_check_command"], "codex login status")  # type: ignore[index]
            self.assertEqual(data["runners"]["planner"]["doctor"]["auth_check_command"], "codex login status")

    def test_configure_agent_claude_command_sets_auth_status_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            result = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker_fallback",
                "--role",
                "worker",
                "--adapter",
                "claude_code_cli",
                "--command",
                "claude",
                "--json",
                env=env,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            home_payload = self.read_home_local_config(root / "loopplane-home")
            project_key = data["local_overrides"]["project_key"]
            worker = home_payload["projects"][project_key]["runners"]["worker_fallback"]  # type: ignore[index]
            self.assertEqual(worker["doctor"]["check_command"], "claude --version")  # type: ignore[index]
            self.assertEqual(worker["doctor"]["auth_check_command"], "claude auth status")  # type: ignore[index]
            self.assertEqual(data["runners"]["worker_fallback"]["doctor"]["auth_check_command"], "claude auth status")

    def test_doctor_agent_named_runner_reports_ok_for_available_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            configure = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--json",
                env=env,
            )
            self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)
            text_result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", env=env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["runner_results"][0]["runner_id"], "worker")
            self.assertEqual(data["runner_results"][0]["status"], "ok")
            self.assertEqual(text_result.returncode, 0, text_result.stderr + text_result.stdout)
            self.assertIn("Agent runner doctor: ok", text_result.stdout)
            self.assertIn("worker: ok", text_result.stdout)

    def test_doctor_agent_all_checks_every_configured_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            for runner_id, role, adapter, command in (
                ("worker", "worker", "codex_cli", sys.executable),
                ("worker_fallback", "worker", "claude_code_cli", sys.executable),
                ("inspector", "inspector", "noop", "noop"),
            ):
                configure = self.run_loopplane(
                    "configure-agent",
                    "--project",
                    str(project),
                    "--runner",
                    runner_id,
                    "--role",
                    role,
                    "--adapter",
                    adapter,
                    "--command",
                    command,
                    "--json",
                    env=env,
                )
                self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--all", "--json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "ok")
            self.assertEqual(
                data["selected_runner_ids"],
                [
                    "auditor",
                    "auditor_fallback",
                    "change_request_planner",
                    "change_request_planner_fallback",
                    "expansion_planner",
                    "expansion_planner_fallback",
                    "final_reviewer",
                    "final_reviewer_fallback",
                    "inspector",
                    "inspector_fallback",
                    "objective_verifier",
                    "objective_verifier_fallback",
                    "planner",
                    "planner_fallback",
                    "summary",
                    "summary_fallback",
                    "validator",
                    "validator_fallback",
                    "worker",
                    "worker_fallback",
                ],
            )
            self.assertTrue(all(item["status"] == "ok" for item in data["runner_results"]))

    def test_doctor_agent_all_treats_disabled_runners_as_optional_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            configure = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                sys.executable,
                "--json",
                env=env,
            )
            self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--all", "--json", env=env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "ok", json.dumps(data, indent=2, sort_keys=True))
            self.assertEqual(data["errors"], [])
            self.assertTrue(any("worker_fallback" in warning or "inspector" in warning for warning in data["warnings"]))

            required = self.run_loopplane("doctor-agent", "--project", str(project), "--required", "--json", env=env)
            self.assertEqual(required.returncode, 0, required.stderr + required.stdout)
            required_data = json.loads(required.stdout)
            self.assertEqual(required_data["status"], "ok")
            self.assertNotIn("worker_fallback", required_data["selected_runner_ids"])
            self.assertIn("inspector", required_data["selected_runner_ids"])

    def test_doctor_agent_returns_nonzero_for_waiting_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            configure = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                "missing-codex-command-for-loopplane",
                "--json",
                env=env,
            )
            self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertEqual(data["status"], "waiting_config")
            self.assertIn("worker", data["errors"][0])
            self.assertTrue(data["next_steps"])
            self.assertIn("loopplane configure-agent", data["next_steps"][0])

    def test_doctor_agent_surfaces_structured_diagnostic_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            fake_cli = project / "fake-cli"
            fake_cli.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import sys",
                        "if sys.argv[1:] == ['--version']:",
                        "    print('bad version', file=sys.stderr)",
                        "    sys.exit(9)",
                        "if sys.argv[1:] == ['auth', 'status']:",
                        "    print('not authenticated', file=sys.stderr)",
                        "    sys.exit(17)",
                        "sys.exit(0)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_cli.chmod(fake_cli.stat().st_mode | 0o111)

            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = self.read_agent_config(project)
            worker = config["runners"]["worker"]  # type: ignore[index]
            worker["command"] = fake_cli.as_posix()  # type: ignore[index]
            worker["doctor"] = {  # type: ignore[index]
                "check_command": f"{fake_cli.as_posix()} --version",
                "requires_auth": False,
            }
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            version_json = self.run_loopplane(
                "doctor-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--json",
                env=env,
            )
            version_text = self.run_loopplane(
                "doctor-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                env=env,
            )

            self.assertNotEqual(version_json.returncode, 0)
            version_data = json.loads(version_json.stdout)
            version_checks = version_data["runner_results"][0]["checks"]
            self.assertTrue(
                any(
                    check["name"] == "version_command"
                    and check["code"] == "version_command_failed"
                    for check in version_checks)
            )
            self.assertIn("version_command_failed", version_text.stdout)

            worker["doctor"] = {  # type: ignore[index]
                "check_command": fake_cli.as_posix(),
                "requires_auth": True,
                "auth_check_command": f"{fake_cli.as_posix()} auth status",
            }
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            auth_json = self.run_loopplane(
                "doctor-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--json",
                env=env,
            )

            self.assertNotEqual(auth_json.returncode, 0)
            auth_checks = json.loads(auth_json.stdout)["runner_results"][0]["checks"]
            self.assertTrue(
                any(
                    check["name"] == "authentication"
                    and check["code"] == "authentication_unavailable"
                    for check in auth_checks)
            )

    def test_agent_cli_returns_nonzero_for_unknown_runner_missing_adapter_and_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)

            unknown_runner = self.run_loopplane(
                "doctor-agent",
                "--project",
                str(project),
                "--runner",
                "missing_runner",
                "--json",
                env=env,
            )
            self.assertNotEqual(unknown_runner.returncode, 0)
            self.assertIn("unknown runner", "\n".join(json.loads(unknown_runner.stdout)["errors"]))

            bad_adapter = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "missing_adapter",
                "--command",
                "missing",
                "--json",
                env=env,
            )
            self.assertNotEqual(bad_adapter.returncode, 0)
            self.assertIn("unknown adapter", "\n".join(json.loads(bad_adapter.stdout)["errors"]))

            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = self.read_agent_config(project)
            config["runners"]["worker"]["adapter"] = "missing_adapter"  # type: ignore[index]
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            missing_adapter = self.run_loopplane(
                "doctor-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--json",
                env=env,
            )
            self.assertNotEqual(missing_adapter.returncode, 0)
            missing_adapter_data = json.loads(missing_adapter.stdout)
            self.assertEqual(missing_adapter_data["runner_results"][0]["status"], "waiting_config")
            self.assertIn("not registered", missing_adapter_data["runner_results"][0]["message"])

            config_path.write_text('{"schema_version": "1.5", "runners": []}\n', encoding="utf-8")
            invalid_config = self.run_loopplane("configure-agent", "--project", str(project), "--json", env=env)
            self.assertNotEqual(invalid_config.returncode, 0)
            self.assertEqual(json.loads(invalid_config.stdout)["status"], "waiting_config")

    def test_doctor_agent_reports_missing_local_override_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = self.read_agent_config(project)
            config["runners"]["worker"]["command"] = "missing-codex-command-for-loopplane"  # type: ignore[index]
            config["runners"]["worker"]["doctor"] = {  # type: ignore[index]
                "check_command": "missing-codex-command-for-loopplane --version",
                "requires_auth": False,
            }
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)

            self.assertNotEqual(result.returncode, 0)
            checks = json.loads(result.stdout)["runner_results"][0]["checks"]
            self.assertTrue(
                any(
                    check["name"] == "local_override" and check["code"] == "local_override_missing"
                    for check in checks
                )
            )

    def test_doctor_agent_flags_obvious_adapter_command_family_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            fake_claude = root / "claude"
            fake_claude.write_text("#!/usr/bin/env python3\nprint('fake claude')\n", encoding="utf-8")
            fake_claude.chmod(0o755)
            configure = self.run_loopplane(
                "configure-agent",
                "--project",
                str(project),
                "--runner",
                "worker",
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                str(fake_claude),
                "--json",
                env=env,
            )
            self.assertEqual(configure.returncode, 0, configure.stderr + configure.stdout)

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            checks = data["runner_results"][0]["checks"]
            self.assertEqual(data["runner_results"][0]["status"], "waiting_config")
            self.assertTrue(
                any(
                    check["name"] == "adapter_command_family"
                    and check["code"] == "adapter_command_family_mismatch"
                    for check in checks
                ),
                json.dumps(data, indent=2, sort_keys=True),
            )

    def test_doctor_agent_reports_stale_machine_runner_lock_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            env = self.env_for_home(root)
            home = root / "loopplane-home"
            lock_key = "shared_doctor_lock"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = self.read_agent_config(project)
            runner = config["runners"]["worker"]  # type: ignore[index]
            runner["adapter"] = "noop"  # type: ignore[index]
            runner["command"] = "noop"  # type: ignore[index]
            runner["prompt_delivery"] = {"mode": "stdin"}  # type: ignore[index]
            runner["doctor"] = {"check_command": "noop --version", "requires_auth": False}  # type: ignore[index]
            runner["resource_policy"] = {  # type: ignore[index]
                "global_concurrency_limit": 1,
                "lock_scope": "machine",
                "lock_key": lock_key,
                "queue_when_busy": True,
            }
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "lock_type": "runner_resource",
                        "lock_scope": "machine",
                        "lock_key": lock_key,
                        "lock_path": lock_path.as_posix(),
                        "global_concurrency_limit": 1,
                        "queue_when_busy": True,
                        "run_id": "run_dead",
                        "workflow_id": "wf_dead",
                        "runner_id": "worker",
                        "role": "worker",
                        "pid": 99999999,
                        "acquired_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)
            text_result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", env=env)

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            checks = data["runner_results"][0]["checks"]
            self.assertEqual(data["status"], "waiting_config")
            self.assertTrue(
                any(
                    check["name"] == "runner_resource_lock"
                    and check["code"] == "stale_runner_resource_lock"
                    and "Remove stale lock" in check["message"]
                    for check in checks
                )
            )
            self.assertIn("stale_runner_resource_lock", text_result.stdout)
            self.assertIn("Remove stale lock", text_result.stdout)
            self.assertTrue(lock_path.is_file())

    def test_invalid_loopplane_home_runner_override_is_reported_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = self.init_project(root)
            home = root / "loopplane-home"
            env = self.env_for_home(root)
            local_path = home / "runners" / "agent_runners.local.json"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "runners": {"missing_runner": {"command": "missing"}},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane("doctor-agent", "--project", str(project), "--runner", "worker", "--json", env=env)

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertFalse(data["ok"])
            self.assertIn("cannot define a runner", "\n".join(data["errors"]))
            self.assertIn("missing_runner", local_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
