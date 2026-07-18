from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.adapters.base import AdapterInput, AdapterOutput
from runtime.adapters.claude_code_cli_adapter import ClaudeCodeCliAdapter, ClaudeStreamRenderer
from runtime.adapters.codex_cli_adapter import CodexCliAdapter
from runtime.adapters.noop_adapter import NoopAdapter
from runtime.adapters.registry import (
    ADAPTER_CLASSES,
    available_adapter_names,
    get_adapter,
    register_adapter,
)
from runtime.adapters.shell_adapter import POLICY_BLOCKED_EXIT_CODE, ShellAdapter, build_shell_invocation
from runtime.agent_runners import RunnerConfig
from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config


def runner_config(
    *,
    adapter: str,
    command: str,
    role: str = "worker",
    args: tuple[str, ...] = (),
    prompt_delivery: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    adapter_options: dict[str, object] | None = None,
    permission_policy: dict[str, object] | None = None,
    doctor: dict[str, object] | None = None,
    resource_policy: dict[str, object] | None = None,
    timeout_seconds: int = 10,
) -> RunnerConfig:
    return RunnerConfig(
        runner_id=f"{adapter}_{role}",
        role=role,
        adapter=adapter,
        command=command,
        cwd=".",
        prompt_delivery=prompt_delivery or {"mode": "stdin"},
        args=args,
        env=env or {},
        adapter_options=adapter_options or {},
        timeout_seconds=timeout_seconds,
        stream_logs=True,
        permission_policy=permission_policy
        or {
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": True,
            "read_only": False,
        },
        doctor=doctor or {"check_command": f"{command} --version", "requires_auth": False},
        enabled=True,
        resource_policy=resource_policy,
    )


def adapter_input(root: Path, config: RunnerConfig, *, prompt_content: str = "Prompt text.\n") -> AdapterInput:
    prompt_path = root / "prompt.md"
    prompt_path.write_text(prompt_content, encoding="utf-8")
    return AdapterInput.from_runner_config(
        run_id="run_test",
        workflow_id="wf_test",
        runner_config=config,
        prompt_path=prompt_path,
        prompt_content=prompt_content,
        scheduler_run_dir=root / "runtime" / "run_test",
        role_output_dir=root / "results" / "run_test",
        task_id="T001" if config.role == "worker" else None,
        task_evidence_run_dir=root / "results" / "run_test" if config.role == "worker" else None,
        cwd=str(root),
    )


def produced_paths(result: AdapterOutput) -> set[str]:
    return {path.as_posix() for path in result.produced_files}


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def assert_common_contract(
    testcase: unittest.TestCase,
    result: AdapterOutput,
    root: Path,
    *,
    expected_prompt_content: str = "Prompt text.\n",
) -> None:
    scheduler_run_dir = root / "runtime" / "run_test"
    required = {
        (scheduler_run_dir / "adapter_input.json").as_posix(),
        (scheduler_run_dir / "adapter_result.json").as_posix(),
        (scheduler_run_dir / "stdout.log").as_posix(),
        (scheduler_run_dir / "stderr.log").as_posix(),
        (scheduler_run_dir / "final.md").as_posix(),
    }
    testcase.assertTrue((scheduler_run_dir / "adapter_input.json").is_file())
    testcase.assertLessEqual(required, produced_paths(result))
    saved_input = AdapterInput.read_json(scheduler_run_dir / "adapter_input.json")
    testcase.assertEqual(saved_input.prompt_path, root / "prompt.md")
    testcase.assertEqual(saved_input.prompt_content, expected_prompt_content)
    testcase.assertEqual(saved_input.timeout_seconds, 10)
    saved = AdapterOutput.read_json(result.adapter_result_path)
    testcase.assertEqual(saved.to_dict(), result.to_dict())


def init_monorepo_adapter_workspace(tmp: str) -> tuple[Path, Path, Path, WorkflowPaths, dict[str, object]]:
    repo = Path(tmp) / "monorepo"
    service_a = repo / "service-a"
    service_b = repo / "service-b"
    service_a.mkdir(parents=True)
    service_b.mkdir()
    completed = subprocess.run(["git", "init", "-q", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr + completed.stdout)
    init_project(service_a, "Adapter boundary policy.")
    workflow = load_workflow_config(service_a)
    paths = WorkflowPaths.from_config(service_a, workflow)
    return repo, service_a, service_b, paths, workflow


def write_adapter_boundary_plan(
    paths: WorkflowPaths,
    workflow: dict[str, object],
    *,
    allow_path: str | None = None,
) -> None:
    allow_lines = ""
    if allow_path is not None:
        allow_lines = f"  - allow_out_of_boundary_writes: true\n  - out_of_boundary_write_paths: {allow_path}\n"
    paths.plan_file.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Adapter Boundary

- [ ] T001: Produce adapter result
  - acceptance: Adapter result exists.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 1
  - approval: not_required
{allow_lines}  - deliverables: artifacts/result.txt.
""",
        encoding="utf-8",
    )


def allow_adapter_out_of_boundary_path(project: Path, paths: WorkflowPaths, relative_path: str) -> None:
    workspace_path = project / ".loopplane" / "workspace.json"
    workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
    workspace["allow_out_of_boundary_writes"] = True
    workspace_path.write_text(json.dumps(workspace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    security_path = paths.config_file("security.json")
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["file_access"]["allow_out_of_boundary_writes"] = True
    security["file_access"]["out_of_boundary_write_allowlist"] = [relative_path]
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_sibling_write_script(project: Path, filename: str) -> Path:
    script = project / f"write_{filename}.py"
    script.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            project = Path.cwd()
            target = project.parent / "service-b" / {filename!r}
            target.write_text("adapter wrote sibling\\n", encoding="utf-8")
            Path(__import__("os").environ["LOOPPLANE_FINAL_OUTPUT"]).write_text("adapter completed\\n", encoding="utf-8")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


class ConcreteAdapterTest(unittest.TestCase):
    def test_noop_adapter_writes_contract_outputs_without_external_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="noop", command="noop")
            result = NoopAdapter().run(adapter_input(root, config))

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertFalse(result.adapter_metadata["external_execution"])
            assert_common_contract(self, result, root)
            self.assertTrue(result.stdout_path.is_file())
            self.assertTrue(result.stderr_path.is_file())
            self.assertTrue(result.final_output_path.is_file())
            self.assertTrue(result.adapter_result_path.is_file())
            saved = AdapterOutput.read_json(result.adapter_result_path)
            self.assertEqual(saved.to_dict(), result.to_dict())

    def test_noop_doctor_is_ok_without_command_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="noop", command="missing-noop-command")
            result = NoopAdapter().doctor(adapter_input(root, config))

            self.assertEqual(result.status, "ok")
            self.assertFalse(result.adapter_metadata["external_execution"])

    def test_shell_adapter_runs_stdin_prompt_and_preserves_process_final_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import os, pathlib, sys; "
                "prompt = sys.stdin.read(); "
                "pathlib.Path(os.environ['LOOPPLANE_FINAL_OUTPUT']).write_text('FINAL:' + prompt, encoding='utf-8'); "
                "print('OUT:' + os.environ['CUSTOM']); "
                "print('ERR', file=sys.stderr)"
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=("-c", script),
                env={"CUSTOM": "value"},
            )
            result = ShellAdapter().run(adapter_input(root, config, prompt_content="Hello adapter.\n"))

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "OUT:value\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "ERR\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "FINAL:Hello adapter.\n")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["policy_decision"]["allowed"], True)

    def test_shell_adapter_streams_stdout_to_log_before_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = (
                "import os, pathlib, sys, time; "
                "stdout_path = pathlib.Path(os.environ['LOOPPLANE_STDOUT_LOG']); "
                "final_path = pathlib.Path(os.environ['LOOPPLANE_FINAL_OUTPUT']); "
                "print('stream marker', flush=True); "
                "deadline = time.time() + 3; "
                "seen = False; "
                "\nwhile time.time() < deadline:\n"
                "    if stdout_path.exists() and 'stream marker' in stdout_path.read_text(encoding='utf-8', errors='replace'):\n"
                "        seen = True\n"
                "        break\n"
                "    time.sleep(0.05)\n"
                "final_path.write_text('seen=' + str(seen) + '\\n', encoding='utf-8')\n"
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=("-c", script),
            )

            result = ShellAdapter().run(adapter_input(root, config, prompt_content="Hello adapter.\n"))

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "stream marker\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "seen=True\n")

    def test_shell_adapter_terminates_summary_process_group_after_completion_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            role_output = root / "results" / "run_test"
            child_pid_path = root / "child.pid"
            script = (
                "import json, pathlib, subprocess, sys, time; "
                f"role_output = pathlib.Path({role_output.as_posix()!r}); "
                f"child_pid_path = pathlib.Path({child_pid_path.as_posix()!r}); "
                "role_output.mkdir(parents=True, exist_ok=True); "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "child_pid_path.write_text(str(child.pid), encoding='utf-8'); "
                "(role_output / 'agent_status.json').write_text(json.dumps({'status': 'completed'}) + '\\n', encoding='utf-8'); "
                "time.sleep(30)"
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                role="summary",
                args=("-c", script),
                adapter_options={"completion_marker_grace_seconds": 0},
                timeout_seconds=20,
            )

            started = time.monotonic()
            result = ShellAdapter().run(adapter_input(root, config, prompt_content="Summarize.\n"))
            elapsed = time.monotonic() - started

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertLess(elapsed, 5)
            self.assertTrue(result.adapter_metadata["terminated_after_completion_marker"])
            self.assertEqual(result.adapter_metadata["termination_reason"], "completion_marker")
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3
            while _pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_pid_exists(child_pid))

    def test_shell_adapter_blocks_unreported_out_of_boundary_write_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, service_b, paths, workflow = init_monorepo_adapter_workspace(tmp)
            write_adapter_boundary_plan(paths, workflow)
            script = write_sibling_write_script(service_a, "adapter_oob_default.txt")
            config = runner_config(adapter="shell", command=sys.executable, args=(script.as_posix(),))

            result = ShellAdapter().run(adapter_input(service_a, config, prompt_content="Write sibling.\n"))

            result_dict = result.to_dict()
            policy = result_dict["adapter_metadata"]["workspace_boundary_policy"]
            self.assertEqual(result.exit_code, POLICY_BLOCKED_EXIT_CODE)
            self.assertTrue((service_b / "adapter_oob_default.txt").is_file())
            self.assertFalse(policy["ok"], json.dumps(policy, indent=2, sort_keys=True))
            self.assertEqual(policy["status"], "violation")
            self.assertIn("../service-b/adapter_oob_default.txt", json.dumps(policy, sort_keys=True))
            self.assertIn("../service-b/adapter_oob_default.txt", result_dict["produced_files"])
            self.assertIn("workspace boundary policy", result.final_output_path.read_text(encoding="utf-8"))

    def test_shell_adapter_allows_explicit_out_of_boundary_write_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _repo, service_a, service_b, paths, workflow = init_monorepo_adapter_workspace(tmp)
            target = service_b / "adapter_oob_allowed.txt"
            relative_target = os.path.relpath(target, start=service_a).replace(os.sep, "/")
            allow_adapter_out_of_boundary_path(service_a, paths, relative_target)
            write_adapter_boundary_plan(paths, workflow, allow_path=relative_target)
            script = write_sibling_write_script(service_a, target.name)
            config = runner_config(adapter="shell", command=sys.executable, args=(script.as_posix(),))

            result = ShellAdapter().run(adapter_input(service_a, config, prompt_content="Write allowed sibling.\n"))

            result_dict = result.to_dict()
            policy = result_dict["adapter_metadata"]["workspace_boundary_policy"]
            self.assertEqual(result.exit_code, 0, json.dumps(policy, indent=2, sort_keys=True))
            self.assertTrue(target.is_file())
            self.assertTrue(policy["ok"], json.dumps(policy, indent=2, sort_keys=True))
            self.assertEqual(policy["status"], "pass")
            self.assertIn(relative_target, json.dumps(policy, sort_keys=True))
            self.assertIn(relative_target, result_dict["produced_files"])
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "adapter completed\n")

    def test_shell_adapter_acquires_machine_runner_lock_under_loopplane_home_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            observed_path = root / "observed.json"
            lock_key = "shared_shell_runner"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = root / "observe_lock.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import pathlib

                    lock_path = pathlib.Path(os.environ["EXPECTED_LOCK_PATH"])
                    observed = {
                        "exists_during_run": lock_path.is_file(),
                        "lock_path": lock_path.as_posix(),
                    }
                    if lock_path.is_file():
                        observed["metadata"] = json.loads(lock_path.read_text(encoding="utf-8"))
                    pathlib.Path(os.environ["OBSERVED_PATH"]).write_text(
                        json.dumps(observed, sort_keys=True),
                        encoding="utf-8",
                    )
                    pathlib.Path(os.environ["LOOPPLANE_FINAL_OUTPUT"]).write_text("lock observed\\n", encoding="utf-8")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=(script.as_posix(),),
                env={
                    "EXPECTED_LOCK_PATH": lock_path.as_posix(),
                    "OBSERVED_PATH": observed_path.as_posix(),
                },
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
            )

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                result = ShellAdapter().run(adapter_input(root, config, prompt_content="Lock test.\n"))

            observed = json.loads(observed_path.read_text(encoding="utf-8"))
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(observed["exists_during_run"], observed)
            self.assertEqual(observed["metadata"]["lock_scope"], "machine")
            self.assertEqual(observed["metadata"]["lock_key"], lock_key)
            self.assertEqual(observed["metadata"]["runner_id"], "shell_worker")
            self.assertEqual(observed["metadata"]["run_id"], "run_test")
            self.assertFalse(lock_path.exists())

    def test_shell_adapter_releases_machine_runner_lock_after_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            observed_path = root / "observed_failure.json"
            lock_key = "shared_failure_runner"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = root / "observe_then_fail.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import pathlib
                    import sys

                    lock_path = pathlib.Path(os.environ["EXPECTED_LOCK_PATH"])
                    pathlib.Path(os.environ["OBSERVED_PATH"]).write_text(
                        json.dumps({"exists_during_run": lock_path.is_file()}, sort_keys=True),
                        encoding="utf-8",
                    )
                    sys.exit(42)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=(script.as_posix(),),
                env={
                    "EXPECTED_LOCK_PATH": lock_path.as_posix(),
                    "OBSERVED_PATH": observed_path.as_posix(),
                },
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
            )

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                result = ShellAdapter().run(adapter_input(root, config, prompt_content="Failure lock test.\n"))

            observed = json.loads(observed_path.read_text(encoding="utf-8"))
            self.assertEqual(result.exit_code, 42)
            self.assertTrue(observed["exists_during_run"], observed)
            self.assertFalse(lock_path.exists())

    def test_shell_adapter_releases_machine_runner_lock_after_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            observed_path = root / "observed_timeout.json"
            lock_key = "shared_timeout_runner"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = root / "observe_then_sleep.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import pathlib
                    import time

                    lock_path = pathlib.Path(os.environ["EXPECTED_LOCK_PATH"])
                    pathlib.Path(os.environ["OBSERVED_PATH"]).write_text(
                        json.dumps({"exists_during_run": lock_path.is_file()}, sort_keys=True),
                        encoding="utf-8",
                    )
                    time.sleep(5)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=(script.as_posix(),),
                env={
                    "EXPECTED_LOCK_PATH": lock_path.as_posix(),
                    "OBSERVED_PATH": observed_path.as_posix(),
                },
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
                timeout_seconds=1,
            )

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                result = ShellAdapter().run(adapter_input(root, config, prompt_content="Timeout lock test.\n"))

            observed = json.loads(observed_path.read_text(encoding="utf-8"))
            self.assertTrue(result.timed_out)
            self.assertTrue(observed["exists_during_run"], observed)
            self.assertFalse(lock_path.exists())

    def test_shell_adapter_workspace_scope_resource_policy_stays_out_of_loopplane_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            lock_key = "workspace_local_runner"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = root / "workspace_scope.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import os
                    import pathlib

                    pathlib.Path(os.environ["LOOPPLANE_FINAL_OUTPUT"]).write_text("workspace scoped\\n", encoding="utf-8")
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=(script.as_posix(),),
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "workspace",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
            )

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                result = ShellAdapter().run(adapter_input(root, config, prompt_content="Workspace lock test.\n"))

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(lock_path.exists())
            self.assertNotIn("runner_resource_lock", result.adapter_metadata)

    def test_shell_adapter_serializes_multi_workspace_machine_lock_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            workspace_a = root / "workspace-a"
            workspace_b = root / "workspace-b"
            workspace_a.mkdir()
            workspace_b.mkdir()
            active_marker = root / "active.marker"
            violation_marker = root / "overlap.violation"
            lock_key = "shared_contention_runner"
            lock_path = home / "locks" / "runner_locks" / f"{lock_key}.lock"
            script = root / "protected_runner.py"
            script.write_text(
                textwrap.dedent(
                    """
                    import os
                    import pathlib
                    import time

                    active = pathlib.Path(os.environ["ACTIVE_MARKER"])
                    violation = pathlib.Path(os.environ["VIOLATION_MARKER"])
                    fd = None
                    try:
                        fd = os.open(active, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        os.write(fd, b"active\\n")
                    except FileExistsError:
                        violation.write_text("overlap\\n", encoding="utf-8")
                    try:
                        time.sleep(0.4)
                        pathlib.Path(os.environ["LOOPPLANE_FINAL_OUTPUT"]).write_text("protected runner finished\\n", encoding="utf-8")
                    finally:
                        if fd is not None:
                            os.close(fd)
                            active.unlink(missing_ok=True)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            config = runner_config(
                adapter="shell",
                command=sys.executable,
                args=(script.as_posix(),),
                env={
                    "ACTIVE_MARKER": active_marker.as_posix(),
                    "VIOLATION_MARKER": violation_marker.as_posix(),
                },
                resource_policy={
                    "global_concurrency_limit": 1,
                    "lock_scope": "machine",
                    "lock_key": lock_key,
                    "queue_when_busy": True,
                },
                timeout_seconds=5,
            )
            results: list[object] = []

            def run_in_workspace(workspace: Path) -> None:
                try:
                    results.append(ShellAdapter().run(adapter_input(workspace, config, prompt_content="Contention test.\n")))
                except BaseException as error:
                    results.append(error)

            with patch.dict("os.environ", {"LOOPPLANE_HOME": home.as_posix()}):
                threads = [
                    threading.Thread(target=run_in_workspace, args=(workspace_a,)),
                    threading.Thread(target=run_in_workspace, args=(workspace_b,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertEqual(len(results), 2)
            for result in results:
                if isinstance(result, BaseException):
                    raise AssertionError(result) from result
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.adapter_metadata["runner_resource_lock"]["lock_path"], lock_path.as_posix())
            self.assertFalse(violation_marker.exists())
            self.assertFalse(active_marker.exists())
            self.assertFalse(lock_path.exists())

    def test_shell_adapter_builds_configured_prompt_delivery_invocations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = (
                (
                    "stdin",
                    {"mode": "stdin"},
                    lambda argv, stdin_text, prompt_path: (
                        self.assertEqual(argv, ["fake-cli", "--base"]),
                        self.assertEqual(stdin_text, "Prompt text.\n"),
                    ),
                ),
                (
                    "file_argument",
                    {"mode": "file_argument", "argument_template": "--input={{prompt_path}}"},
                    lambda argv, stdin_text, prompt_path: (
                        self.assertEqual(argv, ["fake-cli", "--base", f"--input={prompt_path.as_posix()}"]),
                        self.assertIsNone(stdin_text),
                    ),
                ),
                (
                    "stdin_or_prompt_flag_file",
                    {"mode": "stdin_or_prompt_flag", "prompt_flag": "--prompt-file", "prompt_file": "{{prompt_path}}"},
                    lambda argv, stdin_text, prompt_path: (
                        self.assertEqual(argv, ["fake-cli", "--base", "--prompt-file", prompt_path.as_posix()]),
                        self.assertIsNone(stdin_text),
                    ),
                ),
                (
                    "stdin_or_prompt_flag_stdin",
                    {"mode": "stdin_or_prompt_flag"},
                    lambda argv, stdin_text, prompt_path: (
                        self.assertEqual(argv, ["fake-cli", "--base"]),
                        self.assertEqual(stdin_text, "Prompt text.\n"),
                    ),
                ),
            )
            for name, prompt_delivery, assertions in cases:
                with self.subTest(name=name):
                    case_root = root / name
                    case_root.mkdir()
                    config = runner_config(
                        adapter="shell",
                        command="fake-cli",
                        args=("--base",),
                        prompt_delivery=prompt_delivery,
                    )
                    current_input = adapter_input(case_root, config)

                    argv, stdin_text = build_shell_invocation(current_input)

                    assertions(argv, stdin_text, current_input.prompt_path)

    def test_shell_family_doctors_wait_for_unrepresentable_prompt_delivery_modes(self) -> None:
        adapters = (ShellAdapter(), CodexCliAdapter(), ClaudeCodeCliAdapter())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                for mode in ("interactive_terminal", "custom_adapter"):
                    with self.subTest(adapter=adapter.adapter_name, mode=mode):
                        case_root = root / f"{adapter.adapter_name}_{mode}"
                        case_root.mkdir()
                        config = runner_config(
                            adapter=adapter.adapter_name,
                            command=sys.executable,
                            prompt_delivery={"mode": mode},
                        )

                        result = adapter.doctor(adapter_input(case_root, config))

                        self.assertEqual(result.status, "waiting_config")
                        prompt_checks = [check for check in result.checks if check.get("name") == "prompt_delivery"]
                        self.assertEqual(len(prompt_checks), 1)
                        self.assertEqual(prompt_checks[0]["status"], "waiting_config")
                        self.assertIn(mode, prompt_checks[0]["message"])

    def test_shell_adapter_blocks_disallowed_worker_command_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="shell", command="git", args=("commit", "-m", "blocked"))
            result = ShellAdapter().run(adapter_input(root, config))

            self.assertEqual(result.exit_code, POLICY_BLOCKED_EXIT_CODE)
            self.assertIn("write-oriented", result.stderr_path.read_text(encoding="utf-8"))
            self.assertFalse(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["policy_decision"]["decision"], "blocked_git_write")

    def test_codex_cli_blocks_when_command_execution_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            execution_marker = root / "executed.txt"
            fake_codex = root / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import pathlib",
                        f"pathlib.Path({str(execution_marker)!r}).write_text('executed', encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)
            config = runner_config(
                adapter="codex_cli",
                command=fake_codex.as_posix(),
                permission_policy={
                    "allow_project_file_edit": True,
                    "allow_command_execution": False,
                    "require_approval_for_risky_commands": True,
                    "read_only": False,
                },
            )

            result = CodexCliAdapter().run(adapter_input(root, config))

            self.assertEqual(result.exit_code, POLICY_BLOCKED_EXIT_CODE)
            self.assertFalse(execution_marker.exists())
            self.assertFalse(result.adapter_metadata["external_execution"])
            self.assertEqual(
                result.adapter_metadata["policy_decision"]["decision"],
                "command_execution_disabled",
            )

    def test_cli_adapters_block_disallowed_worker_command_before_process_execution(self) -> None:
        adapters = (ClaudeCodeCliAdapter(),)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / adapter.adapter_name
                    case_root.mkdir()
                    execution_marker = case_root / "executed.txt"
                    fake_git = case_root / "git"
                    fake_git.write_text(
                        "\n".join(
                            [
                                f"#!{sys.executable}",
                                "import pathlib",
                                f"pathlib.Path({str(execution_marker)!r}).write_text('executed', encoding='utf-8')",
                                "print('should not run')",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    fake_git.chmod(fake_git.stat().st_mode | 0o111)
                    config = runner_config(
                        adapter=adapter.adapter_name,
                        command=fake_git.as_posix(),
                        args=("commit", "-m", "blocked"),
                    )

                    result = adapter.run(adapter_input(case_root, config))

                    self.assertEqual(result.exit_code, POLICY_BLOCKED_EXIT_CODE)
                    self.assertFalse(execution_marker.exists())
                    self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "")
                    self.assertIn("write-oriented", result.stderr_path.read_text(encoding="utf-8"))
                    self.assertEqual(
                        result.final_output_path.read_text(encoding="utf-8"),
                        "Command blocked by permission policy.\n",
                    )
                    self.assertFalse(result.adapter_metadata["external_execution"])
                    self.assertEqual(result.adapter_metadata["policy_decision"]["allowed"], False)
                    self.assertEqual(result.adapter_metadata["policy_decision"]["decision"], "blocked_git_write")
                    self.assertEqual(result.adapter_metadata["policy_decision"]["operation"], "commit")
                    saved = json.loads(result.adapter_result_path.read_text(encoding="utf-8"))
                    self.assertFalse(saved["adapter_metadata"]["external_execution"])
                    self.assertEqual(
                        saved["adapter_metadata"]["policy_decision"]["decision"],
                        "blocked_git_write",
                    )

    def test_shell_doctor_reports_missing_command_as_waiting_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="shell", command="missing-shell-command-for-loopplane")
            result = ShellAdapter().doctor(adapter_input(root, config))

            self.assertEqual(result.status, "waiting_config")
            self.assertTrue(any(check["name"] == "command_exists" for check in result.checks))

    def test_shell_doctor_checks_full_configured_argv_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="shell", command="git", args=("commit", "-m", "blocked"))
            result = ShellAdapter().doctor(adapter_input(root, config))

            self.assertEqual(result.status, "waiting_config")
            self.assertTrue(
                any(
                    check["name"] == "permission_policy" and check["decision"] == "blocked_git_write"
                    for check in result.checks
                )
            )

    def test_cli_doctors_report_policy_mismatch_for_blocked_configured_command(self) -> None:
        adapters = (ClaudeCodeCliAdapter(),)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / f"{adapter.adapter_name}_doctor"
                    case_root.mkdir()
                    fake_git = case_root / "git"
                    fake_git.write_text(f"#!{sys.executable}\nprint('doctor only')\n", encoding="utf-8")
                    fake_git.chmod(fake_git.stat().st_mode | 0o111)
                    config = runner_config(
                        adapter=adapter.adapter_name,
                        command=fake_git.as_posix(),
                        args=("commit", "-m", "blocked"),
                    )

                    result = adapter.doctor(adapter_input(case_root, config))

                    self.assertEqual(result.status, "waiting_config")
                    self.assertTrue(result.adapter_metadata["process_execution"])
                    policy_checks = [
                        check
                        for check in result.checks
                        if check.get("name") == "permission_policy"
                    ]
                    self.assertEqual(len(policy_checks), 1)
                    self.assertEqual(policy_checks[0]["status"], "waiting_config")
                    self.assertEqual(policy_checks[0]["decision"], "blocked_git_write")
                    self.assertEqual(policy_checks[0]["code"], "policy_mismatch")

    def test_cli_doctors_distinguish_missing_command(self) -> None:
        adapters = (CodexCliAdapter(), ClaudeCodeCliAdapter())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / adapter.adapter_name
                    case_root.mkdir()
                    config = runner_config(
                        adapter=adapter.adapter_name,
                        command=f"missing-{adapter.adapter_name}-command-for-loopplane",
                    )

                    result = adapter.doctor(adapter_input(case_root, config))

                    self.assertEqual(result.status, "waiting_config")
                    command_checks = [check for check in result.checks if check.get("name") == "command_exists"]
                    self.assertEqual(len(command_checks), 1)
                    self.assertEqual(command_checks[0]["status"], "waiting_config")
                    self.assertEqual(command_checks[0]["code"], "command_missing")
                    self.assertFalse(any(check.get("name") == "version_command" for check in result.checks))

    def test_cli_doctors_distinguish_version_command_failure(self) -> None:
        adapters = (CodexCliAdapter(), ClaudeCodeCliAdapter())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / f"{adapter.adapter_name}_version"
                    case_root.mkdir()
                    fake_cli = case_root / "fake-cli"
                    fake_cli.write_text(
                        "\n".join(
                            [
                                f"#!{sys.executable}",
                                "import sys",
                                "print('version broke', file=sys.stderr)",
                                "sys.exit(23)",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    fake_cli.chmod(fake_cli.stat().st_mode | 0o111)
                    config = runner_config(adapter=adapter.adapter_name, command=fake_cli.as_posix())

                    result = adapter.doctor(adapter_input(case_root, config))

                    self.assertEqual(result.status, "waiting_config")
                    version_checks = [check for check in result.checks if check.get("name") == "version_command"]
                    self.assertEqual(len(version_checks), 1)
                    self.assertEqual(version_checks[0]["status"], "waiting_config")
                    self.assertEqual(version_checks[0]["code"], "version_command_failed")
                    self.assertEqual(version_checks[0]["exit_code"], 23)
                    self.assertIn("version broke", version_checks[0]["stderr_excerpt"])

    def test_cli_doctors_distinguish_authentication_unavailable(self) -> None:
        adapters = (CodexCliAdapter(), ClaudeCodeCliAdapter())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / f"{adapter.adapter_name}_auth"
                    case_root.mkdir()
                    fake_cli = case_root / "fake-cli"
                    fake_cli.write_text(
                        "\n".join(
                            [
                                f"#!{sys.executable}",
                                "import sys",
                                "if sys.argv[1:] == ['--version']:",
                                "    print('fake version')",
                                "    sys.exit(0)",
                                "if sys.argv[1:] == ['auth', 'status']:",
                                "    print('not logged in', file=sys.stderr)",
                                "    sys.exit(42)",
                                "sys.exit(0)",
                            ]
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    fake_cli.chmod(fake_cli.stat().st_mode | 0o111)
                    config = runner_config(
                        adapter=adapter.adapter_name,
                        command=fake_cli.as_posix(),
                        doctor={
                            "check_command": f"{fake_cli.as_posix()} --version",
                            "requires_auth": True,
                            "auth_check_command": f"{fake_cli.as_posix()} auth status",
                        },
                    )

                    result = adapter.doctor(adapter_input(case_root, config))

                    self.assertEqual(result.status, "waiting_config")
                    auth_checks = [check for check in result.checks if check.get("name") == "authentication"]
                    self.assertEqual(len(auth_checks), 1)
                    self.assertEqual(auth_checks[0]["status"], "waiting_config")
                    self.assertEqual(auth_checks[0]["code"], "authentication_unavailable")
                    self.assertEqual(auth_checks[0]["exit_code"], 42)
                    self.assertIn("not logged in", auth_checks[0]["stderr_excerpt"])

    def test_cli_doctors_distinguish_unwritable_output_directory(self) -> None:
        adapters = (CodexCliAdapter(), ClaudeCodeCliAdapter())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for adapter in adapters:
                with self.subTest(adapter=adapter.adapter_name):
                    case_root = root / f"{adapter.adapter_name}_output"
                    case_root.mkdir()
                    blocked_parent = case_root / "blocked-parent"
                    blocked_parent.write_text("not a directory\n", encoding="utf-8")
                    prompt_path = case_root / "prompt.md"
                    prompt_path.write_text("doctor prompt\n", encoding="utf-8")
                    config = runner_config(adapter=adapter.adapter_name, command=sys.executable)
                    current_input = AdapterInput.from_runner_config(
                        run_id="run_test",
                        workflow_id="wf_test",
                        runner_config=config,
                        prompt_path=prompt_path,
                        prompt_content="doctor prompt\n",
                        scheduler_run_dir=blocked_parent / "runtime",
                        role_output_dir=case_root / "results",
                        task_id="T001",
                        task_evidence_run_dir=case_root / "results",
                        cwd=str(case_root),
                    )

                    result = adapter.doctor(current_input)

                    self.assertEqual(result.status, "waiting_config")
                    output_checks = [
                        check
                        for check in result.checks
                        if check.get("name") == "output_directory"
                        and check.get("code") == "output_directory_unwritable"
                    ]
                    self.assertEqual(len(output_checks), 1)
                    self.assertEqual(output_checks[0]["path_kind"], "scheduler_run_dir")

    def test_codex_cli_doctor_inspects_configuration_without_task_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="codex_cli", command="missing-codex-command-for-loopplane")
            result = CodexCliAdapter().doctor(adapter_input(root, config))

            self.assertEqual(result.status, "waiting_config")
            self.assertTrue(result.adapter_metadata["process_execution"])
            self.assertFalse(result.adapter_metadata["external_execution"])
            self.assertTrue(any(check["name"] == "command_exists" for check in result.checks))

    def test_codex_cli_adapter_executes_fake_codex_with_file_argument_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "codex"
            fake_codex.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import os",
                        "import pathlib",
                        "import sys",
                        "prompt = sys.stdin.read() if '-' in sys.argv[1:] else pathlib.Path(sys.argv[-1]).read_text(encoding='utf-8')",
                        "final_path = pathlib.Path(os.environ['LOOPPLANE_FINAL_OUTPUT'])",
                        "final_path.write_text('CODEX FINAL:' + prompt, encoding='utf-8')",
                        "evidence_dir = pathlib.Path(os.environ['LOOPPLANE_TASK_EVIDENCE_RUN_DIR'])",
                        "(evidence_dir / 'fake_codex_seen.txt').write_text(prompt, encoding='utf-8')",
                        "print('CODEX STDOUT:' + os.environ['LOOPPLANE_RUN_ID'])",
                        "print('CODEX STDERR', file=sys.stderr)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | 0o111)
            config = runner_config(
                adapter="codex_cli",
                command=fake_codex.as_posix(),
                prompt_delivery={"mode": "file_argument"},
            )

            result = CodexCliAdapter().run(adapter_input(root, config, prompt_content="Run the Codex adapter.\n"))

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.adapter, "codex_cli")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["delivery_mode"], "file_argument")
            self.assertEqual(result.adapter_metadata["policy_decision"]["allowed"], True)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "CODEX STDOUT:run_test\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CODEX STDERR\n")
            self.assertEqual(result.final_output_path.read_text(encoding="utf-8"), "CODEX FINAL:Run the Codex adapter.\n")
            self.assertEqual(
                (root / "results" / "run_test" / "fake_codex_seen.txt").read_text(encoding="utf-8"),
                "Run the Codex adapter.\n",
            )
            assert_common_contract(self, result, root, expected_prompt_content="Run the Codex adapter.\n")
            self.assertLessEqual(
                {
                    (root / "results" / "run_test" / "fake_codex_seen.txt").as_posix(),
                    result.adapter_result_path.as_posix(),
                },
                produced_paths(result),
            )
            self.assertTrue(result.adapter_result_path.is_file())
            saved = AdapterOutput.read_json(result.adapter_result_path)
            self.assertEqual(saved.to_dict(), result.to_dict())

    def test_codex_cli_does_not_inject_json_log_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="codex_cli", command="codex")
            invocation, _stdin = CodexCliAdapter().build_invocation(adapter_input(root, config))

            self.assertNotIn("--json", invocation)

    def test_codex_cli_preserves_explicit_json_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="codex_cli", command="codex", args=("--json",))
            invocation, _stdin = CodexCliAdapter().build_invocation(adapter_input(root, config))

            self.assertEqual(invocation.count("--json"), 1)

    def test_codex_cli_translates_legacy_effort_flag_to_config_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(
                adapter="codex_cli",
                command="codex",
                args=("--effort", "max"),
                adapter_options={"reasoning_effort": "xhigh"},
            )

            invocation, _stdin = CodexCliAdapter().build_invocation(adapter_input(root, config))

            self.assertNotIn("--effort", invocation)
            self.assertIn("-c", invocation)
            self.assertIn('model_reasoning_effort="max"', invocation)
            self.assertNotIn('model_reasoning_effort="xhigh"', invocation)

    def test_claude_code_cli_doctor_inspects_configuration_without_task_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="claude_code_cli", command="missing-claude-command-for-loopplane")
            result = ClaudeCodeCliAdapter().doctor(adapter_input(root, config))

            self.assertEqual(result.status, "waiting_config")
            self.assertTrue(result.adapter_metadata["process_execution"])
            self.assertFalse(result.adapter_metadata["external_execution"])
            self.assertTrue(any(check["name"] == "command_exists" for check in result.checks))

    def test_claude_code_cli_adapter_executes_fake_claude_with_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_claude = root / "claude"
            fake_claude.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import os",
                        "import pathlib",
                        "import sys",
                        "prompt_args = [arg for arg in sys.argv[1:] if not arg.startswith('--')]",
                        "prompt_path = pathlib.Path(prompt_args[-1])",
                        "prompt = prompt_path.read_text(encoding='utf-8')",
                        "final_path = pathlib.Path(os.environ['LOOPPLANE_FINAL_OUTPUT'])",
                        "final_path.write_text('CLAUDE FINAL:' + prompt, encoding='utf-8')",
                        "evidence_dir = pathlib.Path(os.environ['LOOPPLANE_TASK_EVIDENCE_RUN_DIR'])",
                        "(evidence_dir / 'fake_claude_seen.txt').write_text(prompt, encoding='utf-8')",
                        "print('CLAUDE STDOUT:' + os.environ['LOOPPLANE_RUN_ID'])",
                        "print('CLAUDE STDERR', file=sys.stderr)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_claude.chmod(fake_claude.stat().st_mode | 0o111)
            config = runner_config(
                adapter="claude_code_cli",
                command=fake_claude.as_posix(),
                prompt_delivery={"mode": "stdin_or_prompt_flag", "prompt_file": "{{prompt_path}}"},
            )

            result = ClaudeCodeCliAdapter().run(adapter_input(root, config, prompt_content="Run the Claude adapter.\n"))

            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.adapter, "claude_code_cli")
            self.assertTrue(result.adapter_metadata["external_execution"])
            self.assertEqual(result.adapter_metadata["delivery_mode"], "stdin_or_prompt_flag")
            self.assertEqual(result.adapter_metadata["policy_decision"]["allowed"], True)
            self.assertEqual(result.stdout_path.read_text(encoding="utf-8"), "CLAUDE STDOUT:run_test\n")
            self.assertEqual(result.stderr_path.read_text(encoding="utf-8"), "CLAUDE STDERR\n")
            self.assertEqual(
                result.final_output_path.read_text(encoding="utf-8"),
                "CLAUDE FINAL:Run the Claude adapter.\n",
            )
            self.assertEqual(
                (root / "results" / "run_test" / "fake_claude_seen.txt").read_text(encoding="utf-8"),
                "Run the Claude adapter.\n",
            )
            self.assertIn("--dangerously-skip-permissions", result.adapter_metadata["argv"])
            assert_common_contract(self, result, root, expected_prompt_content="Run the Claude adapter.\n")

    def test_claude_code_cli_permission_mode_can_be_left_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(
                adapter="claude_code_cli",
                command="claude",
                adapter_options={"claude_permission_mode": "default"},
            )
            invocation, _stdin = ClaudeCodeCliAdapter().build_invocation(adapter_input(root, config))

            self.assertEqual(invocation[0], "claude")
            self.assertNotIn("--dangerously-skip-permissions", invocation)
            self.assertNotIn("--permission-mode", invocation)

    def test_claude_code_cli_injects_streaming_output_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(adapter="claude_code_cli", command="claude")
            invocation, _stdin = ClaudeCodeCliAdapter().build_invocation(adapter_input(root, config))

            self.assertIn("--print", invocation)
            self.assertIn("--output-format=stream-json", invocation)
            self.assertIn("--verbose", invocation)
            # Single-token form keeps the policy classifier from mistaking the
            # value for a positional subcommand.
            self.assertNotIn("stream-json", invocation)

    def test_claude_code_cli_streaming_flags_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(
                adapter="claude_code_cli",
                command="claude",
                adapter_options={"claude_stream_logs": False},
            )
            invocation, _stdin = ClaudeCodeCliAdapter().build_invocation(adapter_input(root, config))

            self.assertNotIn("--output-format=stream-json", invocation)
            self.assertNotIn("--output-format", invocation)

    def test_claude_code_cli_respects_explicit_output_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = runner_config(
                adapter="claude_code_cli",
                command="claude",
                args=("--output-format", "json"),
            )
            invocation, _stdin = ClaudeCodeCliAdapter().build_invocation(adapter_input(root, config))

            self.assertNotIn("--output-format=stream-json", invocation)
            self.assertEqual(invocation.count("--output-format"), 1)

    def test_claude_stream_renderer_compacts_events_and_captures_final(self) -> None:
        renderer = ClaudeStreamRenderer(result_preview_chars=40)
        events = [
            {"type": "system", "subtype": "init", "model": "opus-test"},
            {"type": "stream_event", "event": {"x": "y" * 5000}},  # token noise: dropped
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Reading the file."}]}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/tmp/data.txt"}}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "content": "X" * 8000}]},  # bloat: clipped
            },
            {"type": "result", "subtype": "success", "result": "It has 200 lines.", "duration_ms": 9000, "total_cost_usd": 0.05},
        ]
        raw = "\n".join(json.dumps(e) for e in events)
        rendered = "\n".join(line for line in (renderer.render_line(l) for l in raw.splitlines()) if line is not None)

        self.assertIn("▶ session start · model=opus-test", rendered)
        self.assertIn("Reading the file.", rendered)
        self.assertIn("🔧 Read(/tmp/data.txt)", rendered)
        # The 8000-byte tool result is summarized to a size + clipped preview.
        self.assertIn("↳ result (8000b):", rendered)
        self.assertIn("…(+", rendered)
        self.assertIn("✓ done · 9s · $0.0500", rendered)
        # The token-streaming noise event is dropped entirely.
        self.assertNotIn("stream_event", rendered)
        # Rendered output is dramatically smaller than the raw stream.
        self.assertLess(len(rendered), len(raw) / 10)
        # Final answer is captured for final.md.
        self.assertEqual(renderer.final_output(), "It has 200 lines.")

    def test_claude_stream_renderer_falls_back_to_assistant_text(self) -> None:
        # No terminal result line (process killed mid-turn): recover partial text.
        renderer = ClaudeStreamRenderer()
        events = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}},
        ]
        for event in events:
            renderer.render_line(json.dumps(event))
        self.assertEqual(renderer.final_output(), "first\nsecond")

    def test_claude_stream_renderer_passes_through_non_json(self) -> None:
        # Text-output mode or an error banner: keep the line verbatim.
        renderer = ClaudeStreamRenderer()
        self.assertEqual(renderer.render_line("API Error: Internal server error"), "API Error: Internal server error")
        self.assertIsNone(renderer.render_line("   "))
        self.assertEqual(renderer.final_output(), "")

    def test_claude_stream_renderer_can_drop_result_preview(self) -> None:
        # preview_chars=0 keeps the size marker but omits the content entirely.
        renderer = ClaudeStreamRenderer(result_preview_chars=0)
        line = renderer.render_line(
            json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "secret payload"}]}})
        )
        self.assertEqual(line, "   ↳ result (14b)")
        self.assertNotIn("secret", line)

    def test_claude_adapter_makes_transform_only_when_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            streaming = runner_config(adapter="claude_code_cli", command="claude")
            self.assertIsInstance(
                ClaudeCodeCliAdapter().make_stdout_transform(adapter_input(root, streaming)),
                ClaudeStreamRenderer,
            )
            disabled = runner_config(
                adapter="claude_code_cli",
                command="claude",
                adapter_options={"claude_stream_logs": False},
            )
            self.assertIsNone(ClaudeCodeCliAdapter().make_stdout_transform(adapter_input(root, disabled)))

    def test_registry_resolves_builtin_adapters(self) -> None:
        names = available_adapter_names()

        self.assertIn("noop", names)
        self.assertIn("shell", names)
        self.assertIn("codex_cli", names)
        self.assertIn("claude_code_cli", names)
        self.assertIsInstance(get_adapter("noop"), NoopAdapter)
        self.assertIsInstance(get_adapter("shell"), ShellAdapter)
        self.assertIsInstance(get_adapter("codex_cli"), CodexCliAdapter)
        self.assertIsInstance(get_adapter("claude_code_cli"), ClaudeCodeCliAdapter)

    def test_registry_supports_custom_adapter_extension_path(self) -> None:
        class LocalCustomAdapter(NoopAdapter):
            adapter_name = "local_custom"

        previous = ADAPTER_CLASSES.get("local_custom")
        try:
            register_adapter("local_custom", LocalCustomAdapter)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = runner_config(
                    adapter="local_custom",
                    command="local-custom",
                    prompt_delivery={"mode": "custom_adapter"},
                )

                result = get_adapter("local_custom").run(adapter_input(root, config))

                self.assertEqual(result.exit_code, 0)
                self.assertIn("local_custom", available_adapter_names())
                self.assertEqual(result.adapter, "local_custom")
                self.assertEqual(result.adapter_metadata["delivery_mode"], "custom_adapter")
        finally:
            if previous is None:
                ADAPTER_CLASSES.pop("local_custom", None)
            else:
                ADAPTER_CLASSES["local_custom"] = previous


if __name__ == "__main__":
    unittest.main()
