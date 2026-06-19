from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from runtime.agent_runners import (
    AgentRunnerConfigError,
    agent_runner_project_key,
    load_agent_runners,
    load_agent_runners_file,
)
from runtime.init_workflow import init_project


def minimal_config() -> dict[str, object]:
    return {
        "schema_version": "1.5",
        "default_runner": "worker",
        "runners": {
            "worker": {
                "role": "worker",
                "adapter": "shell",
                "enabled": True,
                "command": "python3",
                "cwd": "{{project_root}}",
                "prompt_delivery": {"mode": "stdin"},
                "args": ["-m", "worker"],
                "env": {"LOOPPLANE_ROLE": "worker"},
                "timeout_seconds": 3600,
                "stream_logs": True,
                "permission_policy": {
                    "allow_project_file_edit": True,
                    "allow_command_execution": True,
                    "require_approval_for_risky_commands": True,
                    "read_only": False,
                },
                "doctor": {
                    "check_command": "python3 --version",
                    "requires_auth": False,
                },
            }
        },
    }


def write_config(path: Path, config: dict[str, object]) -> None:
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class AgentRunnerConfigTest(unittest.TestCase):
    def test_loads_inherited_runner_with_child_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["planner"] = {  # type: ignore[index]
                "inherits": "worker",
                "role": "planner",
                "timeout_seconds": 1200,
            }
            write_config(config_file, config)

            loaded = load_agent_runners_file(config_file)

            planner = loaded.runner("planner")
            self.assertEqual(planner.role, "planner")
            self.assertEqual(planner.adapter, "shell")
            self.assertEqual(planner.command, "python3")
            self.assertEqual(planner.cwd, "{{project_root}}")
            self.assertEqual(planner.prompt_delivery["mode"], "stdin")
            self.assertEqual(planner.timeout_seconds, 1200)
            self.assertEqual(planner.env["LOOPPLANE_ROLE"], "worker")
            self.assertEqual(planner.inherits, "worker")

    def test_missing_standard_agent_runners_inherit_default_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            write_config(config_file, config)

            loaded = load_agent_runners_file(config_file)

            for runner_id, role in {
                "planner": "planner",
                "auditor": "auditor",
                "validator": "validator",
                "change_request_planner": "change_request_planner",
                "expansion_planner": "expansion_planner",
                "objective_verifier": "objective_verifier",
                "summary": "summary",
                "final_reviewer": "final_reviewer",
                "inspector": "inspector",
            }.items():
                runner = loaded.runner(runner_id)
                self.assertEqual(runner.role, role)
                self.assertTrue(runner.enabled)
                self.assertEqual(runner.command, "python3")

    def test_claude_agent_runners_follow_claude_worker_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            worker = json.loads(json.dumps(config["runners"]["worker"]))  # type: ignore[index]
            worker["adapter"] = "claude_code_cli"
            worker["command"] = "claude"
            worker["enabled"] = True
            config["runners"]["claude_worker"] = worker  # type: ignore[index]
            write_config(config_file, config)

            loaded = load_agent_runners_file(config_file)

            self.assertTrue(loaded.runner("claude_summary").enabled)
            self.assertEqual(loaded.runner("claude_summary").role, "summary")
            self.assertEqual(loaded.runner("claude_final_reviewer").command, "claude")

    def test_loads_runner_failover_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            worker = config["runners"]["worker"]  # type: ignore[index]
            backup = json.loads(json.dumps(worker))
            backup["command"] = "claude"
            config["runners"]["backup_worker"] = backup  # type: ignore[index]
            config["runner_failover"] = {
                "worker": {
                    "strategy": "ordered",
                    "runners": ["worker", "backup_worker"],
                    "mark_unhealthy_after": 4,
                    "failure_window_seconds": 900,
                }
            }
            write_config(config_file, config)

            loaded = load_agent_runners_file(config_file)

            rule = loaded.runner_failover["worker"]
            self.assertEqual(rule["strategy"], "ordered")
            self.assertEqual(rule["runners"], ("worker", "backup_worker"))
            self.assertEqual(rule["mark_unhealthy_after"], 4)
            self.assertEqual(rule["failure_window_seconds"], 900)

    def test_rejects_runner_failover_unknown_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runner_failover"] = {
                "worker": {
                    "strategy": "ordered",
                    "runners": ["worker", "missing_worker"],
                    "mark_unhealthy_after": 4,
                    "failure_window_seconds": 900,
                }
            }
            write_config(config_file, config)

            with self.assertRaisesRegex(AgentRunnerConfigError, "missing_worker"):
                load_agent_runners_file(config_file)

    def test_child_permission_policy_deep_merges_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["inspector"] = {  # type: ignore[index]
                "inherits": "worker",
                "role": "inspector",
                "permission_policy": {
                    "allow_project_file_edit": False,
                    "allow_command_execution": False,
                    "read_only": True,
                },
            }
            write_config(config_file, config)

            inspector = load_agent_runners_file(config_file).runner("inspector")

            self.assertFalse(inspector.permission_policy["allow_project_file_edit"])
            self.assertFalse(inspector.permission_policy["allow_command_execution"])
            self.assertTrue(inspector.permission_policy["read_only"])
            self.assertTrue(inspector.permission_policy["require_approval_for_risky_commands"])

    def test_resource_policy_resolves_through_inheritance_and_as_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["worker"]["resource_policy"] = {  # type: ignore[index]
                "global_concurrency_limit": 1,
                "lock_scope": "machine",
                "lock_key": "codex_cli_default",
                "queue_when_busy": True,
            }
            config["runners"]["planner"] = {  # type: ignore[index]
                "inherits": "worker",
                "role": "planner",
                "resource_policy": {
                    "lock_key": "planner_gpu",
                    "queue_when_busy": False,
                },
            }
            write_config(config_file, config)

            loaded = load_agent_runners_file(config_file)

            planner = loaded.runner("planner")
            self.assertEqual(
                planner.resource_policy,
                {
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": "planner_gpu",
                    "queue_when_busy": False,
                },
            )
            self.assertEqual(planner.as_dict()["resource_policy"], dict(planner.resource_policy or {}))
            self.assertEqual(planner.as_dict(include_inherits=False)["resource_policy"]["lock_key"], "planner_gpu")

    def test_local_resource_policy_override_deep_merges_without_mutating_portable_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Resource policy local override loading.")
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["runners"]["worker"]["resource_policy"] = {
                "global_concurrency_limit": 1,
                "lock_scope": "machine",
                "lock_key": "codex_cli_default",
                "queue_when_busy": True,
            }
            write_config(config_path, config)
            before = config_path.read_text(encoding="utf-8")
            local_path = project / ".loopplane" / "config" / "local" / "agent_runners.local.json"
            local_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "runners": {
                            "worker": {
                                "resource_policy": {
                                    "lock_key": "project_local_gpu",
                                    "queue_when_busy": False,
                                }
                            }
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_agent_runners(project)

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertEqual(
                loaded.runner("worker").resource_policy,
                {
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": "project_local_gpu",
                    "queue_when_busy": False,
                },
            )

    def test_rejects_invalid_resource_policy_with_field_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["worker"]["resource_policy"] = {  # type: ignore[index]
                "global_concurrency_limit": 0,
                "lock_scope": "global",
                "lock_key": "../codex",
                "queue_when_busy": "yes",
                "extra": True,
            }
            write_config(config_file, config)

            with self.assertRaises(AgentRunnerConfigError) as caught:
                load_agent_runners_file(config_file)
            errors = "\n".join(caught.exception.errors)
            self.assertIn("resource_policy has unknown fields: extra", errors)
            self.assertIn("resource_policy.global_concurrency_limit must be a positive integer", errors)
            self.assertIn("resource_policy.lock_scope must be one of machine, workspace", errors)
            self.assertIn("resource_policy.lock_key must be a filename-safe non-empty string", errors)
            self.assertIn("resource_policy.queue_when_busy must be boolean", errors)

    def test_rejects_incomplete_resource_policy_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["worker"]["resource_policy"] = {"lock_scope": "machine"}  # type: ignore[index]
            write_config(config_file, config)

            with self.assertRaises(AgentRunnerConfigError) as caught:
                load_agent_runners_file(config_file)
            errors = "\n".join(caught.exception.errors)
            self.assertIn("resource_policy missing 'global_concurrency_limit'", errors)
            self.assertIn("resource_policy missing 'lock_key'", errors)
            self.assertIn("resource_policy missing 'queue_when_busy'", errors)

    def test_agent_runners_schema_accepts_resource_policy_and_rejects_bad_values(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "runtime" / "schemas" / "agent_runners.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        config = {
            "schema_version": "1.5",
            "default_runner": "worker",
            "runner_failover": {
                "worker": {
                    "strategy": "ordered",
                    "runners": ["worker", "worker_fallback"],
                    "mark_unhealthy_after": 4,
                    "failure_window_seconds": 900,
                }
            },
            "runners": {
                runner_id: {}
                for runner_id in ("worker", "worker_fallback", "planner", "auditor", "inspector")
            },
        }
        config["runners"]["worker"]["resource_policy"] = {
            "global_concurrency_limit": 1,
            "lock_scope": "machine",
            "lock_key": "codex_cli_default",
            "queue_when_busy": True,
        }

        self.assertEqual([], [error.message for error in validator.iter_errors(config)])

        invalid = json.loads(json.dumps(config))
        invalid["runners"]["worker"]["resource_policy"]["lock_scope"] = "global"
        invalid["runners"]["worker"]["resource_policy"]["lock_key"] = "../codex"
        invalid["runners"]["worker"]["resource_policy"]["queue_when_busy"] = "yes"
        invalid["runner_failover"]["worker"]["mark_unhealthy_after"] = 0
        messages = sorted(error.message for error in validator.iter_errors(invalid))
        self.assertTrue(any("'global' is not one of" in message for message in messages))
        self.assertTrue(any("'../codex' does not match" in message for message in messages))
        self.assertTrue(any("'yes' is not of type 'boolean'" in message for message in messages))
        self.assertTrue(any("0 is less than the minimum of 1" in message for message in messages))

    def test_rejects_missing_required_resolved_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            del config["runners"]["worker"]["command"]  # type: ignore[index]
            write_config(config_file, config)

            with self.assertRaisesRegex(AgentRunnerConfigError, "missing required field 'command'"):
                load_agent_runners_file(config_file)

    def test_rejects_unknown_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["planner"] = {"inherits": "missing_parent", "role": "planner"}  # type: ignore[index]
            write_config(config_file, config)

            with self.assertRaisesRegex(AgentRunnerConfigError, "inherits unknown parent 'missing_parent'"):
                load_agent_runners_file(config_file)

    def test_rejects_inheritance_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["worker"]["inherits"] = "planner"  # type: ignore[index]
            config["runners"]["planner"] = {"inherits": "worker", "role": "planner"}  # type: ignore[index]
            write_config(config_file, config)

            with self.assertRaisesRegex(AgentRunnerConfigError, "inheritance cycle detected"):
                load_agent_runners_file(config_file)

    def test_loads_custom_adapter_prompt_delivery_for_registered_extension_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "agent_runners.json"
            config = minimal_config()
            config["runners"]["worker"]["adapter"] = "local_custom_adapter"  # type: ignore[index]
            config["runners"]["worker"]["prompt_delivery"] = {"mode": "custom_adapter"}  # type: ignore[index]
            write_config(config_file, config)

            runner = load_agent_runners_file(config_file).runner("worker")

            self.assertEqual(runner.adapter, "local_custom_adapter")
            self.assertEqual(runner.prompt_delivery["mode"], "custom_adapter")

    def test_loads_default_init_config_through_project_loader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Agent runner loading.")

            loaded = load_agent_runners(project)

            self.assertEqual(loaded.default_runner, "worker")
            self.assertEqual(loaded.runner().runner_id, "worker")
            self.assertEqual(loaded.runner("planner").command, "codex")
            self.assertEqual(loaded.runner("planner").timeout_seconds, 21600)
            self.assertEqual(loaded.runner("auditor").prompt_delivery["mode"], "file_argument")
            self.assertEqual(loaded.runner("change_request_planner").role, "change_request_planner")
            self.assertEqual(loaded.runner("change_request_planner").command, "codex")
            self.assertEqual(loaded.runner("summary").role, "summary")
            self.assertFalse(loaded.runner("summary").enabled)
            self.assertEqual(loaded.runner("summary").command, "codex")
            self.assertTrue(loaded.runner("validator").enabled)
            self.assertTrue(loaded.runner("final_reviewer").enabled)
            self.assertTrue(loaded.runner("inspector").permission_policy["allow_command_execution"])
            self.assertEqual(loaded.template_variables["project_root"], ".")

    def test_loads_project_local_machine_override_without_mutating_portable_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Project-local runner override loading.")
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            before = config_path.read_text(encoding="utf-8")
            local_path = project / ".loopplane" / "config" / "local" / "agent_runners.local.json"
            local_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "runners": {
                            "planner": {
                                "command": "/opt/local/bin/codex",
                                "doctor": {
                                    "check_command": "/opt/local/bin/codex --version",
                                    "requires_auth": True,
                                    "auth_check_command": "/opt/local/bin/codex login status",
                                },
                            }
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = load_agent_runners(project)

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertEqual(loaded.runner("planner").command, "/opt/local/bin/codex")
            self.assertEqual(loaded.runner("planner").doctor["check_command"], "/opt/local/bin/codex --version")
            self.assertEqual(loaded.local_override_paths, (local_path.resolve(),))
            self.assertEqual(loaded.local_override_runner_ids, ("planner",))

    def test_loads_loopplane_home_project_scoped_override_with_project_local_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "home"
            init_project(project, "LOOPPLANE_HOME runner override loading.")
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            before = config_path.read_text(encoding="utf-8")
            key = agent_runner_project_key(project)
            home_local = home / "runners" / "agent_runners.local.json"
            home_local.parent.mkdir(parents=True, exist_ok=True)
            home_local.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "projects": {
                            key: {
                                "project_root": project.resolve().as_posix(),
                                "runners": {
                                    "codex_worker": {
                                        "command": "/machine/home/codex",
                                        "doctor": {
                                            "check_command": "/machine/home/codex --version",
                                            "requires_auth": True,
                                        },
                                    }
                                },
                            }
                        },
                        "runners": {},
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            project_local = project / ".loopplane" / "config" / "local" / "agent_runners.local.json"
            project_local.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "runners": {
                            "codex_worker": {
                                "command": "/project/local/codex",
                                "doctor": {
                                    "check_command": "/project/local/codex --version",
                                    "requires_auth": True,
                                },
                            }
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            old_env = os.environ.get("LOOPPLANE_HOME")
            os.environ["LOOPPLANE_HOME"] = str(home)
            try:
                loaded = load_agent_runners(project)
            finally:
                if old_env is None:
                    os.environ.pop("LOOPPLANE_HOME", None)
                else:
                    os.environ["LOOPPLANE_HOME"] = old_env

            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertEqual(loaded.runner("worker").command, "/project/local/codex")
            self.assertEqual(loaded.local_override_paths, (home_local.resolve(), project_local.resolve()))
            self.assertEqual(loaded.local_override_runner_ids, ("worker",))


if __name__ == "__main__":
    unittest.main()
