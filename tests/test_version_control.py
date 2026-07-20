from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence
from unittest import mock

from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.version_control import (
    GitCommandResult,
    SubprocessGitCommandRunner,
    capture_run_git_metadata,
    create_git_checkpoint,
    run_git_doctor,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeGitRunner:
    def __init__(
        self,
        *,
        available: bool = True,
        responses: dict[tuple[str, ...], GitCommandResult] | None = None,
    ) -> None:
        self.available = available
        self.responses = responses or {}
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def git_path(self) -> str | None:
        return "/usr/bin/git" if self.available else None

    def run(self, project_root: Path, args: Sequence[str]) -> GitCommandResult:
        command = tuple(args)
        self.calls.append((project_root, command))
        if command == ("--version",):
            return GitCommandResult(0, "git version fake\n", "")
        if command[:3] == ("status", "--porcelain=v1", "-z"):
            fallback = self.responses.get(("status", "--porcelain=v1", "-z"))
            if fallback is not None:
                return fallback
        return self.responses.get(command, GitCommandResult(128, "", "not a git repository"))


class VersionControlDoctorUnitTest(unittest.TestCase):
    def test_doctor_reports_git_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Git unavailable doctor.")

            result = run_git_doctor(project, runner=FakeGitRunner(available=False))

            self.assertEqual(result["status"], "waiting_config")
            self.assertFalse(result["git"]["available"])
            self.assertFalse(result["repository"]["inside_work_tree"])
            self.assertFalse(result["local_init"]["possible"])
            self.assertEqual(result["local_init"]["reason"], "git_unavailable")
            self.assertTrue(any("Git is unavailable" in error for error in result["errors"]))

    def test_doctor_detects_existing_repo_from_mocked_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Existing repo doctor.")
            fake = FakeGitRunner(
                responses={
                    ("rev-parse", "--is-inside-work-tree"): GitCommandResult(0, "true\n", ""),
                    ("rev-parse", "--show-toplevel"): GitCommandResult(0, f"{project.resolve()}\n", ""),
                    ("rev-parse", "--verify", "HEAD"): GitCommandResult(0, "abc123def456\n", ""),
                    ("status", "--porcelain=v1", "-z"): GitCommandResult(
                        0,
                        " M src/example.py\0?? tests/test_example.py\0",
                        "",
                    ),
                }
            )

            result = run_git_doctor(project, runner=fake)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["git"]["available"])
            self.assertTrue(result["repository"]["inside_work_tree"])
            self.assertEqual(result["repository"]["root"], str(project.resolve()))
            self.assertEqual(result["repository"]["head_commit"], "abc123def456")
            self.assertTrue(result["repository"]["dirty"])
            self.assertEqual(result["repository"]["dirty_files_count"], 2)
            self.assertFalse(result["local_init"]["possible"])
            self.assertEqual(result["local_init"]["reason"], "existing_repository")
            self.assertEqual(result["configuration"]["provider"], "git")
            self.assertEqual(result["configuration"]["checkpoint_backend"], "managed_refs")
            self.assertTrue(result["configuration"]["resolved_refs_namespace"].startswith("refs/loopplane/wf_"))
            self.assertFalse(result["configuration"]["effective_worker_checkpointing"]["enabled"])
            self.assertEqual(result["configuration"]["effective_worker_checkpointing"]["reason"], "checkpoint_policy_disabled")

    def test_doctor_reports_local_init_possible_for_non_repo_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No repo doctor.", git_runner=FakeGitRunner(available=False))
            fake = FakeGitRunner(
                responses={
                    ("rev-parse", "--is-inside-work-tree"): GitCommandResult(
                        128,
                        "",
                        "fatal: not a git repository",
                    ),
                }
            )

            result = run_git_doctor(project, runner=fake)

            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["repository"]["inside_work_tree"])
            self.assertTrue(result["local_init"]["possible"])
            self.assertEqual(result["local_init"]["reason"], "git_init_available")

    def test_doctor_flags_unsafe_and_unsupported_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Unsafe config doctor.")
            config_path = project / ".loopplane" / "config" / "version_control.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["provider"] = "hg"
            config["repository_mode"] = "remote_only"
            config["checkpoint_backend"] = "user_branch"
            config["refs_namespace"] = "refs/heads/main"
            config["no_remote_push"] = False
            config["do_not_switch_user_branch"] = False
            config["do_not_modify_user_index"] = False
            config["commit_policy"]["write_to_user_branch"] = True
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            fake = FakeGitRunner(
                responses={
                    ("rev-parse", "--is-inside-work-tree"): GitCommandResult(128, "", "not a git repository"),
                }
            )

            result = run_git_doctor(project, runner=fake)

            self.assertEqual(result["status"], "waiting_config")
            errors = "\n".join(result["errors"])
            self.assertIn("Unsupported version-control provider", errors)
            self.assertIn("Unsupported repository_mode", errors)
            self.assertIn("Unsupported checkpoint_backend", errors)
            self.assertIn("refs_namespace must resolve under refs/loopplane", errors)
            self.assertIn("no_remote_push must be true", errors)
            self.assertIn("do_not_switch_user_branch must be true", errors)
            self.assertIn("do_not_modify_user_index must be true", errors)
            self.assertIn("write_to_user_branch must be false", errors)

    def test_doctor_user_branch_approval_warning_requires_user_branch_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Managed refs warning doctor.")
            config_path = project / ".loopplane" / "config" / "version_control.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["commit_policy"]["require_approval_for_user_branch_commit"] = True
            config["commit_policy"]["write_to_user_branch"] = False
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            fake = FakeGitRunner(
                responses={
                    ("rev-parse", "--is-inside-work-tree"): GitCommandResult(128, "", "not a git repository"),
                }
            )

            result = run_git_doctor(project, runner=fake)

            warnings = "\n".join(result["warnings"])
            self.assertNotIn("require_approval_for_user_branch_commit is true", warnings)

            config["commit_policy"]["write_to_user_branch"] = True
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = run_git_doctor(project, runner=fake)

            warnings = "\n".join(result["warnings"])
            self.assertIn("require_approval_for_user_branch_commit is true", warnings)


@unittest.skipIf(shutil.which("git") is None, "git is not available")
class VersionControlDoctorCliIntegrationTest(unittest.TestCase):
    def run_loopplane(
        self,
        *args: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LoopPlane), *args],
            cwd=cwd or REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=env,
        )

    def run_git(self, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(project), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def workflow_paths(self, project: Path) -> WorkflowPaths:
        return WorkflowPaths.from_config(project, load_workflow_config(project))

    def enable_before_worker_checkpoint(self, project: Path) -> None:
        config_path = self.workflow_paths(project).version_control_config_file
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["checkpoint_policy"]["before_worker_run"] = True
        config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def test_cli_doctor_reports_non_repo_project_and_init_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Doctor non-repo.", git_runner=FakeGitRunner(available=False))

            doctor = self.run_loopplane("vc", "doctor", "--project", str(project), "--json")

            self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
            data = json.loads(doctor.stdout)
            self.assertTrue(data["git"]["available"])
            self.assertFalse(data["repository"]["inside_work_tree"])
            self.assertTrue(data["local_init"]["possible"])
            self.assertEqual(data["configuration"]["provider"], "git")
            self.assertTrue(data["configuration"]["no_remote_push"])
            self.assertTrue(data["configuration"]["do_not_modify_user_index"])
            self.assertEqual(data["configuration"]["run_metadata"]["enabled"], False)
            self.assertEqual(data["configuration"]["effective_worker_checkpointing"]["reason"], "checkpoint_policy_disabled")

    def test_checkpoint_excludes_project_local_loopplane_home_and_python_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Checkpoint excludes local generated state.")
            env = {**os.environ, "LOOPPLANE_HOME": str(project / ".loopplane_home")}
            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (project / "pkg" / "__pycache__").mkdir(parents=True)
            (project / "pkg" / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"pyc")
            (project / ".loopplane_home" / "runners").mkdir(parents=True)
            (project / ".loopplane_home" / "runners" / "agent_runners.local.json").write_text(
                '{"secret":"local"}\n',
                encoding="utf-8",
            )
            (project / ".loopplane" / "config" / "local").mkdir(parents=True, exist_ok=True)
            (project / ".loopplane" / "config" / "local" / "agent_runners.local.json").write_text(
                '{"secret":"project-local"}\n',
                encoding="utf-8",
            )
            (project / "LOOPPLANE_DASHBOARD.url").write_text(
                "[InternetShortcut]\nURL=http://127.0.0.1:9999/?token=secret\n",
                encoding="utf-8",
            )

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "exclude_generated_state",
                "--json",
                env=env,
            )

            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            payload = json.loads(checkpoint.stdout)
            commit = payload["checkpoint"]["commit"]
            tree_files = self.run_git(project, "ls-tree", "-r", "--name-only", commit).stdout.splitlines()
            self.assertIn("src/app.py", tree_files)
            self.assertFalse(any(path.startswith(".loopplane_home/") for path in tree_files))
            self.assertFalse(any("__pycache__" in path for path in tree_files))
            self.assertFalse(any(path.endswith(".pyc") for path in tree_files))
            self.assertNotIn(".loopplane/config/local/agent_runners.local.json", tree_files)
            self.assertNotIn("LOOPPLANE_DASHBOARD.url", tree_files)

    def test_checkpoint_excludes_large_results_tree_from_candidate_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Checkpoint ignores generated result history.")
            paths = self.workflow_paths(project)
            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            cache_dir = paths.results_dir / "P0.T001" / "runs" / "run_bulk" / "artifacts" / "cache"
            cache_dir.mkdir(parents=True)
            for index in range(2_000):
                (cache_dir / f"shard_{index:05d}.bin").write_bytes(b"generated-cache")

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--json",
            )

            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            payload = json.loads(checkpoint.stdout)
            record = payload["checkpoint"]
            self.assertLess(record["checkpoint_metrics"]["discovered_paths"], 100)
            results_prefix = paths.results_dir.relative_to(project).as_posix() + "/"
            tree_files = self.run_git(
                project,
                "ls-tree",
                "-r",
                "--name-only",
                record["commit"],
            ).stdout.splitlines()
            self.assertIn("src/app.py", tree_files)
            self.assertFalse(any(path.startswith(results_prefix) for path in tree_files))

    def test_checkpoint_repository_probe_does_not_run_unscoped_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Checkpoint repository probe fast path.")
            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            runner = SubprocessGitCommandRunner()

            with mock.patch.object(runner, "run", wraps=runner.run) as run_mock:
                result = create_git_checkpoint(project, reason="manual_checkpoint", runner=runner)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            commands = [tuple(call.args[1]) for call in run_mock.call_args_list]
            self.assertNotIn(("status", "--porcelain=v1", "-z"), commands)
            scoped_statuses = [
                command
                for command in commands
                if command[:3] == ("status", "--porcelain=v1", "-z") and "--" in command
            ]
            self.assertEqual(len(scoped_statuses), 1)

    def test_automatic_checkpoint_budget_exhaustion_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Best-effort checkpoint budget.")
            paths = self.workflow_paths(project)
            config_path = paths.version_control_config_file
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["checkpoint_limits"]["max_paths"] = 1
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            (project / "worker_change.txt").write_text("worker input\n", encoding="utf-8")

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "before_worker_run",
                "--json",
            )

            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            payload = json.loads(checkpoint.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "skipped_budget")
            self.assertEqual(payload["checkpoint"]["status"], "skipped_budget")
            self.assertEqual(payload["checkpoint"]["checkpoint_metrics"]["limit_reason"], "max_paths")
            self.assertNotIn("commit", payload["checkpoint"])
            records = read_jsonl(paths.runtime_dir / "git_checkpoints.jsonl")
            self.assertEqual(records[-1]["status"], "skipped_budget")

    def test_manual_checkpoint_budget_exhaustion_remains_explicit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Manual checkpoint budget failure.")
            paths = self.workflow_paths(project)
            config_path = paths.version_control_config_file
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["checkpoint_limits"]["max_paths"] = 1
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--json",
            )

            self.assertNotEqual(checkpoint.returncode, 0)
            payload = json.loads(checkpoint.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "checkpoint_budget_exceeded")
            self.assertIsNone(payload["checkpoint"])

    def test_cli_doctor_reports_existing_git_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            git_init = subprocess.run(
                ["git", "init"],
                cwd=project,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(git_init.returncode, 0, git_init.stderr + git_init.stdout)
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Doctor existing repo.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

            doctor = self.run_loopplane("vc", "doctor", "--project", str(project), "--json")

            self.assertEqual(doctor.returncode, 0, doctor.stderr + doctor.stdout)
            data = json.loads(doctor.stdout)
            self.assertTrue(data["repository"]["inside_work_tree"])
            self.assertEqual(data["repository"]["root"], str(project.resolve()))
            self.assertFalse(data["local_init"]["possible"])
            self.assertEqual(data["local_init"]["reason"], "existing_repository")
            self.assertEqual(data["checkpoint_model"]["backend"], "managed_refs")
            self.assertIn("managed refs", data["checkpoint_model"]["note"])

            doctor_text = self.run_loopplane("vc", "doctor", "--project", str(project))

            self.assertEqual(doctor_text.returncode, 0, doctor_text.stderr + doctor_text.stdout)
            self.assertIn("LoopPlane version control doctor", doctor_text.stdout)
            self.assertIn("Status: ok", doctor_text.stdout)
            self.assertIn("Git:", doctor_text.stdout)
            self.assertIn("Repository:", doctor_text.stdout)
            self.assertIn("detected: yes", doctor_text.stdout)
            self.assertIn("Configuration:", doctor_text.stdout)
            self.assertIn("checkpoint_backend: managed_refs", doctor_text.stdout)
            self.assertIn("checkpoint_model:", doctor_text.stdout)
            self.assertIn("managed refs", doctor_text.stdout)
            self.assertNotIn("not implemented", doctor_text.stdout)
            self.assertNotIn(".git", doctor_text.stdout)

    def test_cli_doctor_returns_failure_for_unsafe_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Doctor unsafe config.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            config_path = self.workflow_paths(project).version_control_config_file
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["no_remote_push"] = False
            config["commit_policy"]["write_to_user_branch"] = True
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            doctor = self.run_loopplane("vc", "doctor", "--project", str(project), "--json")

            self.assertNotEqual(doctor.returncode, 0)
            data = json.loads(doctor.stdout)
            self.assertEqual(data["status"], "waiting_config")
            errors = "\n".join(data["errors"])
            self.assertIn("no_remote_push must be true", errors)
            self.assertIn("write_to_user_branch must be false", errors)

    def test_cli_status_reports_sanitized_dirty_checkpoint_and_rollback_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Status surface.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            add_initial = self.run_git(project, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(project, "commit", "-m", "initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )
            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--json",
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            checkpoint_payload = json.loads(checkpoint.stdout)
            checkpoint_id = checkpoint_payload["checkpoint"]["checkpoint_id"]
            expected_dirty_count = checkpoint_payload["checkpoint"]["status_entries_after"]

            status_json = self.run_loopplane("vc", "status", "--project", str(project), "--json")
            self.assertEqual(status_json.returncode, 0, status_json.stderr + status_json.stdout)
            self.assertNotIn("not implemented", status_json.stdout)
            self.assertNotIn(".git", status_json.stdout)
            payload = json.loads(status_json.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["enabled"])
            self.assertTrue(payload["git"]["available"])
            self.assertTrue(payload["repository"]["detected"])
            self.assertTrue(payload["dirty"])
            self.assertEqual(payload["changed_files_count"], expected_dirty_count)
            self.assertEqual(payload["repository"]["dirty_files_count"], expected_dirty_count)
            self.assertEqual(payload["last_checkpoint"]["checkpoint_id"], checkpoint_id)
            self.assertNotIn("ref", payload["last_checkpoint"])
            self.assertTrue(payload["rollback"]["available"])
            self.assertFalse(payload["rollback"]["requires_approval"])
            self.assertEqual(payload["checkpoint_model"]["backend"], "managed_refs")
            self.assertIn("managed refs", payload["checkpoint_model"]["note"])

            status_text = self.run_loopplane("vc", "status", "--project", str(project))
            self.assertEqual(status_text.returncode, 0, status_text.stderr + status_text.stdout)
            self.assertIn("LoopPlane version control status", status_text.stdout)
            self.assertIn("Git enabled: yes", status_text.stdout)
            self.assertIn("Repository detected: yes", status_text.stdout)
            self.assertIn(f"Changed files: {expected_dirty_count}", status_text.stdout)
            self.assertIn(f"Last checkpoint: {checkpoint_id}", status_text.stdout)
            self.assertIn("Rollback available: yes", status_text.stdout)
            self.assertIn("Checkpoint model:", status_text.stdout)
            self.assertIn("managed refs", status_text.stdout)
            self.assertNotIn("not implemented", status_text.stdout)
            self.assertNotIn(".git", status_text.stdout)

    def test_cli_status_reports_git_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Status unavailable.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            env = dict(os.environ)
            env["PATH"] = ""

            status = self.run_loopplane("vc", "status", "--project", str(project), "--json", env=env)

            self.assertNotEqual(status.returncode, 0)
            data = json.loads(status.stdout)
            self.assertFalse(data["ok"])
            self.assertFalse(data["git"]["available"])
            self.assertEqual(data["status"], "waiting_config")
            self.assertFalse(data["rollback"]["available"])
            self.assertNotIn(".git", status.stdout)

    def test_cli_status_reports_initialized_non_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Status non-repository.", git_runner=FakeGitRunner(available=False))

            status = self.run_loopplane("vc", "status", "--project", str(project), "--json")

            self.assertNotEqual(status.returncode, 0)
            data = json.loads(status.stdout)
            self.assertTrue(data["git"]["available"])
            self.assertFalse(data["repository"]["detected"])
            self.assertFalse(data["dirty"])
            self.assertEqual(data["changed_files_count"], 0)
            self.assertFalse(data["rollback"]["available"])
            self.assertNotIn(".git", status.stdout)

    def test_cli_checkpoint_creates_managed_ref_without_changing_branch_or_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Checkpoint refs.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            add_initial = self.run_git(project, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(project, "commit", "-m", "initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

            (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )
            add_staged = self.run_git(project, "add", "tests/test_app.py")
            self.assertEqual(add_staged.returncode, 0, add_staged.stderr + add_staged.stdout)

            branch_before = self.run_git(project, "branch", "--show-current").stdout.strip()
            head_before = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
            index_before = self.run_git(project, "ls-files", "--stage", "-z").stdout
            status_before = self.run_git(project, "status", "--short").stdout.splitlines()

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--task",
                "P2.T003",
                "--run",
                "run-test",
                "--json",
            )

            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            data = json.loads(checkpoint.stdout)
            self.assertTrue(data["ok"])
            record = data["checkpoint"]
            ref = record["ref"]
            commit = record["commit"]
            workflow = json.loads(self.workflow_paths(project).workflow_config_file.read_text(encoding="utf-8"))
            self.assertTrue(ref.startswith(f"refs/loopplane/{workflow['workflow_id']}/checkpoints/"))

            show_ref = self.run_git(project, "show-ref", "--verify", ref)
            self.assertEqual(show_ref.returncode, 0, show_ref.stderr + show_ref.stdout)
            self.assertEqual(show_ref.stdout.split()[0], commit)

            branch_after = self.run_git(project, "branch", "--show-current").stdout.strip()
            head_after = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
            index_after = self.run_git(project, "ls-files", "--stage", "-z").stdout
            status_after = self.run_git(project, "status", "--short").stdout.splitlines()

            self.assertEqual(branch_before, branch_after)
            self.assertEqual(head_before, head_after)
            self.assertEqual(index_before, index_after)
            self.assertEqual(record["active_branch_before"], branch_before)
            self.assertEqual(record["active_branch_after"], branch_after)
            self.assertTrue(record["active_branch_unchanged"])
            self.assertTrue(record["head_unchanged"])
            self.assertTrue(record["user_index_unchanged"])
            self.assertEqual(
                self._status_for_paths(status_before, ("src/app.py", "tests/test_app.py")),
                self._status_for_paths(status_after, ("src/app.py", "tests/test_app.py")),
            )
            self.assertNotIn("not implemented", checkpoint.stdout)
            self.assertNotIn(".git", checkpoint.stdout)

            paths = self.workflow_paths(project)
            checkpoint_app = self.run_git(project, "show", f"{commit}:src/app.py")
            checkpoint_test = self.run_git(project, "show", f"{commit}:tests/test_app.py")
            self.assertEqual(checkpoint_app.stdout, "VALUE = 2\n")
            self.assertEqual(checkpoint_test.stdout, "def test_app():\n    assert True\n")

            records = [
                json.loads(line)
                for line in (paths.runtime_dir / "git_checkpoints.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
                if line.strip()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["ref"], ref)
            self.assertEqual(records[0]["commit"], commit)
            self.assertNotIn("included_paths", records[0])
            self.assertNotIn("excluded_paths", records[0])
            self.assertGreaterEqual(records[0]["included_paths_count"], 2)
            self.assertIn("included_paths_sample", records[0])

            vc_status = json.loads(
                (paths.read_models_dir / "version_control_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(vc_status["latest_checkpoint"]["ref"], ref)
            self.assertEqual(vc_status["latest_checkpoint"]["commit"], commit)

            checkpoint_text = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--task",
                "P2.T003",
                "--run",
                "run-test-text",
            )

            self.assertEqual(checkpoint_text.returncode, 0, checkpoint_text.stderr + checkpoint_text.stdout)
            self.assertIn("LoopPlane Git checkpoint", checkpoint_text.stdout)
            self.assertIn("Status: ok", checkpoint_text.stdout)
            self.assertIn("Reason: manual_checkpoint", checkpoint_text.stdout)
            self.assertIn("Active branch unchanged: yes", checkpoint_text.stdout)
            self.assertIn("HEAD unchanged: yes", checkpoint_text.stdout)
            self.assertIn("User index unchanged: yes", checkpoint_text.stdout)
            self.assertIn(f"refs/loopplane/{workflow['workflow_id']}/checkpoints/", checkpoint_text.stdout)
            self.assertNotIn("not implemented", checkpoint_text.stdout)
            self.assertNotIn(".git", checkpoint_text.stdout)

    def test_cli_checkpoint_warns_when_completion_marker_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Post-completion checkpoint warning.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)
            paths = self.workflow_paths(project)
            (paths.runtime_dir / "plan_loop_complete.json").write_text(
                json.dumps({"schema_version": "1.5", "status": "completed"}) + "\n",
                encoding="utf-8",
            )
            (project / "feedback.md").write_text("post-completion note\n", encoding="utf-8")

            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--json",
            )

            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            data = json.loads(checkpoint.stdout)
            self.assertTrue(data["ok"], checkpoint.stdout)
            warning_text = "\n".join(data["warnings"])
            self.assertIn("completion marker already exists", warning_text)
            self.assertIn("final-verify", warning_text)

    def test_cli_run_metadata_reports_text_and_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Run metadata CLI surface.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            add_initial = self.run_git(project, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(project, "commit", "-m", "initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

            paths = self.workflow_paths(project)
            run_dir = paths.results_dir / "T902" / "runs" / "run-metadata-cli"
            pre_json = self.run_loopplane(
                "vc",
                "run-metadata",
                "--project",
                str(project),
                "--run-dir",
                str(run_dir),
                "--stage",
                "pre",
                "--task",
                "T902",
                "--run",
                "run-metadata-cli",
                "--json",
            )

            self.assertEqual(pre_json.returncode, 0, pre_json.stderr + pre_json.stdout)
            self.assertNotIn("not implemented", pre_json.stdout)
            self.assertNotIn(".git", pre_json.stdout)
            pre_payload = json.loads(pre_json.stdout)
            self.assertTrue(pre_payload["ok"])
            self.assertEqual(pre_payload["status"], "ok")
            self.assertEqual(pre_payload["stage"], "pre")
            self.assertEqual(pre_payload["task_id"], "T902")
            self.assertEqual(pre_payload["run_id"], "run-metadata-cli")
            self.assertNotIn("checkpoint", pre_payload["metadata"])

            (project / "src" / "app.py").write_text("VALUE = 2\nEXTRA = 3\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )

            post_text = self.run_loopplane(
                "vc",
                "run-metadata",
                "--project",
                str(project),
                "--run-dir",
                str(run_dir),
                "--stage",
                "post",
                "--task",
                "T902",
                "--run",
                "run-metadata-cli",
            )

            self.assertEqual(post_text.returncode, 0, post_text.stderr + post_text.stdout)
            self.assertIn("LoopPlane run Git metadata", post_text.stdout)
            self.assertIn("Status: ok", post_text.stdout)
            self.assertIn("Stage: post", post_text.stdout)
            self.assertIn("Changed files: 2", post_text.stdout)
            self.assertNotIn("not implemented", post_text.stdout)
            self.assertNotIn(".git", post_text.stdout)

    def test_run_metadata_capture_writes_worker_and_recovery_git_artifacts(self) -> None:
        for run_kind in ("worker", "recovery"):
            with self.subTest(run_kind=run_kind):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    init_result = self.run_loopplane(
                        "init",
                        "--project",
                        str(project),
                        "--brief",
                        f"Run metadata {run_kind}.",
                    )
                    self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

                    for key, value in (
                        ("user.name", "LoopPlane Test"),
                        ("user.email", "loopplane-test@example.invalid"),
                    ):
                        config = self.run_git(project, "config", key, value)
                        self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

                    (project / "src").mkdir()
                    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
                    add_initial = self.run_git(project, "add", ".")
                    self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
                    commit_initial = self.run_git(project, "commit", "-m", "initial")
                    self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)
                    self.enable_before_worker_checkpoint(project)

                    run_id = f"run-{run_kind}"
                    paths = self.workflow_paths(project)
                    run_dir = paths.results_dir / "P2.T004" / "runs" / run_id
                    branch_before = self.run_git(project, "branch", "--show-current").stdout.strip()
                    head_before = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
                    index_before = self.run_git(project, "ls-files", "--stage", "-z").stdout

                    pre = capture_run_git_metadata(
                        project,
                        run_dir,
                        stage="pre",
                        task_id="P2.T004",
                        run_id=run_id,
                        run_kind=run_kind,
                    )
                    self.assertTrue(pre["ok"], json.dumps(pre, indent=2, sort_keys=True))
                    git_dir = run_dir / "git"
                    self.assertEqual((git_dir / "pre_run_head.txt").read_text(encoding="utf-8"), f"{head_before}\n")
                    self.assertTrue((git_dir / "pre_run_status.json").is_file())
                    self.assertTrue((git_dir / "run_metadata_state.json").is_file())
                    checkpoint = pre["metadata"]["checkpoint"]
                    show_ref = self.run_git(project, "show-ref", "--verify", checkpoint["ref"])
                    self.assertEqual(show_ref.returncode, 0, show_ref.stderr + show_ref.stdout)
                    self.assertEqual(show_ref.stdout.split()[0], checkpoint["commit"])
                    expected_run_dir = f"{paths.value('results_dir')}/P2.T004/runs/{run_id}"
                    self.assertEqual(pre["run_dir"], expected_run_dir)
                    self.assertEqual(pre["metadata"]["git_dir"], f"{expected_run_dir}/git")
                    self.assertEqual(pre["metadata"]["pre_run_status_file"], f"{expected_run_dir}/git/pre_run_status.json")
                    self.assertEqual(pre["metadata"]["config_path"], paths.value("version_control_config_file"))
                    self.assertEqual(pre["metadata"]["repository_root"], ".")
                    state = json.loads((git_dir / "run_metadata_state.json").read_text(encoding="utf-8"))
                    self.assertEqual(state["run_dir"], expected_run_dir)
                    checkpoint_records = [
                        json.loads(line)
                        for line in (paths.runtime_dir / "git_checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    self.assertTrue(checkpoint_records)
                    self.assertEqual(checkpoint_records[-1]["repository_root"], ".")
                    vc_status = json.loads((paths.read_models_dir / "version_control_status.json").read_text(encoding="utf-8"))
                    self.assertEqual(vc_status["repository"]["root"], ".")

                    (project / "src" / "app.py").write_text("VALUE = 2\nEXTRA = 3\n", encoding="utf-8")
                    (project / "tests").mkdir()
                    (project / "tests" / "test_app.py").write_text(
                        "def test_app():\n    assert True\n",
                        encoding="utf-8",
                    )

                    post = capture_run_git_metadata(
                        project,
                        run_dir,
                        stage="post",
                        task_id="P2.T004",
                        run_id=run_id,
                        run_kind=run_kind,
                    )
                    self.assertTrue(post["ok"], json.dumps(post, indent=2, sort_keys=True))
                    self.assertEqual(post["run_dir"], expected_run_dir)
                    self.assertEqual(post["metadata"]["git_dir"], f"{expected_run_dir}/git")
                    self.assertEqual(post["metadata"]["repository_root"], ".")
                    self.assertEqual(post["metadata"]["post_run_status_file"], f"{expected_run_dir}/git/post_run_status.json")
                    self.assertEqual(post["metadata"]["changed_files_file"], f"{expected_run_dir}/git/changed_files.json")
                    self.assertEqual(post["metadata"]["project_diff_file"], f"{expected_run_dir}/git/project_diff.patch")

                    for name in (
                        "pre_run_head.txt",
                        "pre_run_status.json",
                        "post_run_status.json",
                        "changed_files.json",
                        "project_diff.patch",
                    ):
                        self.assertTrue((git_dir / name).is_file(), name)

                    post_status = json.loads((git_dir / "post_run_status.json").read_text(encoding="utf-8"))
                    self.assertEqual(post_status["run_kind"], run_kind)
                    self.assertEqual(post_status["phase"], "post_run")
                    self.assertEqual(post_status["repository_root"], ".")
                    self.assertIn("src/app.py", {entry["path"] for entry in post_status["entries"]})

                    changed = json.loads((git_dir / "changed_files.json").read_text(encoding="utf-8"))
                    self.assertEqual(changed["run_kind"], run_kind)
                    self.assertEqual(changed["base_commit"], checkpoint["commit"])
                    by_path = {entry["path"]: entry for entry in changed["changed_files"]}
                    self.assertEqual(by_path["src/app.py"]["change_type"], "modified")
                    self.assertEqual(by_path["src/app.py"]["lines_added"], 2)
                    self.assertEqual(by_path["src/app.py"]["lines_deleted"], 1)
                    self.assertEqual(by_path["src/app.py"]["line_delta"], 1)
                    self.assertEqual(by_path["tests/test_app.py"]["change_type"], "added")
                    self.assertEqual(by_path["tests/test_app.py"]["lines_added"], 2)
                    self.assertEqual(by_path["tests/test_app.py"]["lines_deleted"], 0)
                    self.assertFalse(any(path.startswith(f"{paths.value('results_dir')}/") for path in by_path))
                    self.assertNotIn(f"{paths.value('runtime_dir')}/git_checkpoints.jsonl", by_path)

                    patch = (git_dir / "project_diff.patch").read_text(encoding="utf-8")
                    self.assertIn("diff --git a/src/app.py b/src/app.py", patch)
                    self.assertIn("+EXTRA = 3", patch)
                    self.assertIn("diff --git a/tests/test_app.py b/tests/test_app.py", patch)
                    self.assertNotIn("post_run_status.json", patch)
                    self.assertNotIn("changed_files.json", patch)

                    branch_after = self.run_git(project, "branch", "--show-current").stdout.strip()
                    head_after = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
                    index_after = self.run_git(project, "ls-files", "--stage", "-z").stdout
                    self.assertEqual(branch_before, branch_after)
                    self.assertEqual(head_before, head_after)
                    self.assertEqual(index_before, index_after)

    def test_run_metadata_records_parent_relative_repository_root_for_monorepo_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "monorepo"
            service = repo / "services" / "service-a"
            service.mkdir(parents=True)
            init_repo = subprocess.run(["git", "init", "-q", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(init_repo.returncode, 0, init_repo.stderr + init_repo.stdout)

            init_result = self.run_loopplane("init", "--project", str(service), "--brief", "Monorepo metadata.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            expected_repo_root = os.path.relpath(repo.resolve(), start=service.resolve()).replace(os.sep, "/")
            paths = self.workflow_paths(service)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(service, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            (service / "src").mkdir()
            (service / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            add_initial = self.run_git(service, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(service, "commit", "-m", "initial service state")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)
            self.enable_before_worker_checkpoint(service)

            run_dir = paths.results_dir / "T901" / "runs" / "run-monorepo"
            pre = capture_run_git_metadata(
                service,
                run_dir,
                stage="pre",
                task_id="T901",
                run_id="run-monorepo",
            )

            self.assertTrue(pre["ok"], json.dumps(pre, indent=2, sort_keys=True))
            self.assertEqual(pre["metadata"]["repository_root"], expected_repo_root)
            checkpoint_records = [
                json.loads(line)
                for line in (paths.runtime_dir / "git_checkpoints.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(checkpoint_records)
            self.assertEqual(checkpoint_records[-1]["repository_root"], expected_repo_root)
            vc_status = json.loads((paths.read_models_dir / "version_control_status.json").read_text(encoding="utf-8"))
            self.assertEqual(vc_status["repository"]["root"], expected_repo_root)

    def test_monorepo_checkpoint_and_run_metadata_follow_workspace_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "monorepo"
            service_a = repo / "service-a"
            service_b = repo / "service-b"
            (service_a / "src").mkdir(parents=True)
            service_b.mkdir(parents=True)
            (service_a / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            (service_b / "notes.txt").write_text("baseline sibling\n", encoding="utf-8")
            init_repo = subprocess.run(["git", "init", "-q", str(repo)], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(init_repo.returncode, 0, init_repo.stderr + init_repo.stdout)
            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(repo, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)
            add_initial = self.run_git(repo, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(repo, "commit", "-m", "initial monorepo")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

            init_result = self.run_loopplane("init", "--project", str(service_a), "--brief", "Monorepo boundary.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            paths = self.workflow_paths(service_a)
            (service_a / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            (service_a / "tests").mkdir()
            (service_a / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )
            (service_b / "notes.txt").write_text("dirty sibling tracked change\n", encoding="utf-8")
            (service_b / "scratch.txt").write_text("dirty sibling untracked file\n", encoding="utf-8")
            checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(service_a),
                "--reason",
                "manual_checkpoint",
                "--json",
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
            payload = json.loads(checkpoint.stdout)
            record = payload["checkpoint"]
            commit = record["commit"]
            tree_files = self.run_git(repo, "ls-tree", "-r", "--name-only", commit).stdout.splitlines()
            self.assertIn("service-a/src/app.py", tree_files)
            self.assertIn("service-a/tests/test_app.py", tree_files)
            self.assertFalse(any(path.startswith("service-b/") for path in tree_files))
            self.assertEqual(self.run_git(repo, "show", f"{commit}:service-a/src/app.py").stdout, "VALUE = 2\n")
            self.assertNotEqual(self.run_git(repo, "show", f"{commit}:service-b/notes.txt").returncode, 0)
            self.assertFalse(any("service-b" in path for path in record["included_paths"]))
            self.assertFalse(any("service-b" in path for path in record["excluded_paths"]))
            self.assertGreater(record["status_entries_before"], 0)
            self.assertEqual(record["status_entries_before"], record["status_entries_after"])
            self.assertEqual(record["path_policy"]["workspace_boundary"], "project_root")

            status_json = self.run_loopplane("vc", "status", "--project", str(service_a), "--json")
            self.assertEqual(status_json.returncode, 0, status_json.stderr + status_json.stdout)
            status_payload = json.loads(status_json.stdout)
            self.assertEqual(status_payload["changed_files_count"], record["status_entries_after"])
            self.assertNotIn("service-b", json.dumps(status_payload, sort_keys=True))

            run_id = "run-boundary"
            run_dir = paths.results_dir / "T901" / "runs" / run_id
            pre = capture_run_git_metadata(
                service_a,
                run_dir,
                stage="pre",
                task_id="T901",
                run_id=run_id,
            )
            self.assertTrue(pre["ok"], json.dumps(pre, indent=2, sort_keys=True))
            pre_status = json.loads((run_dir / "git" / "pre_run_status.json").read_text(encoding="utf-8"))
            self.assertFalse(any("service-b" in json.dumps(entry, sort_keys=True) for entry in pre_status["entries"]))

            (service_a / "src" / "app.py").write_text("VALUE = 3\nEXTRA = 4\n", encoding="utf-8")
            (service_b / "notes.txt").write_text("dirty sibling changed again\n", encoding="utf-8")
            post = capture_run_git_metadata(
                service_a,
                run_dir,
                stage="post",
                task_id="T901",
                run_id=run_id,
            )
            self.assertTrue(post["ok"], json.dumps(post, indent=2, sort_keys=True))
            changed = json.loads((run_dir / "git" / "changed_files.json").read_text(encoding="utf-8"))
            changed_paths = {entry["path"] for entry in changed["changed_files"]}
            self.assertIn("src/app.py", changed_paths)
            self.assertFalse(any("service-b" in path for path in changed_paths))
            patch = (run_dir / "git" / "project_diff.patch").read_text(encoding="utf-8")
            self.assertIn("service-a/src/app.py", patch)
            self.assertNotIn("service-b", patch)

    def test_cli_diff_reports_sanitized_task_run_metadata_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Diff metadata surface.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
            self._write_single_task_plan(project, task_id="T900")
            paths = self.workflow_paths(project)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            add_initial = self.run_git(project, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(project, "commit", "-m", "initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)
            self.enable_before_worker_checkpoint(project)

            run_id = "run-diff"
            run_dir = paths.results_dir / "T900" / "runs" / run_id
            pre = capture_run_git_metadata(
                project,
                run_dir,
                stage="pre",
                task_id="T900",
                run_id=run_id,
            )
            self.assertTrue(pre["ok"], json.dumps(pre, indent=2, sort_keys=True))
            (project / "src" / "app.py").write_text("VALUE = 2\nEXTRA = 3\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )
            post = capture_run_git_metadata(
                project,
                run_dir,
                stage="post",
                task_id="T900",
                run_id=run_id,
            )
            self.assertTrue(post["ok"], json.dumps(post, indent=2, sort_keys=True))
            after_checkpoint = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "after_validation_pass",
                "--task",
                "T900",
                "--run",
                run_id,
                "--json",
            )
            self.assertEqual(after_checkpoint.returncode, 0, after_checkpoint.stderr + after_checkpoint.stdout)
            after_payload = json.loads(after_checkpoint.stdout)
            self._write_latest(project, task_id="T900", run_id=run_id, run_dir=run_dir)

            diff_json = self.run_loopplane("vc", "diff", "--project", str(project), "--task", "T900", "--json")
            self.assertEqual(diff_json.returncode, 0, diff_json.stderr + diff_json.stdout)
            self.assertNotIn(".git", diff_json.stdout)
            payload = json.loads(diff_json.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["task_id"], "T900")
            self.assertEqual(payload["run_id"], run_id)
            self.assertEqual(payload["source"], "latest_json")
            self.assertEqual(payload["diff"]["changed_files_count"], 2)
            self.assertTrue(payload["diff"]["patch"]["available"])
            self.assertEqual(
                payload["diff"]["patch"]["path"],
                f"{paths.value('results_dir')}/T900/runs/run-diff/git/project_diff.patch",
            )
            self.assertNotIn("ref", json.dumps(payload["checkpoints"], sort_keys=True))
            self.assertEqual(
                payload["checkpoints"]["before"]["checkpoint_id"],
                pre["metadata"]["checkpoint"]["checkpoint_id"],
            )
            self.assertEqual(
                payload["checkpoints"]["after"]["checkpoint_id"],
                after_payload["checkpoint"]["checkpoint_id"],
            )
            by_path = {entry["path"]: entry for entry in payload["diff"]["changed_files"]}
            self.assertEqual(by_path["src/app.py"]["change_type"], "modified")
            self.assertEqual(by_path["src/app.py"]["lines_added"], 2)
            self.assertEqual(by_path["src/app.py"]["lines_deleted"], 1)
            self.assertEqual(by_path["tests/test_app.py"]["change_type"], "added")
            self.assertFalse(any(path.startswith(f"{paths.value('runtime_dir')}/") for path in by_path))

            diff_text = self.run_loopplane("vc", "diff", "--project", str(project), "--task", "T900")
            self.assertEqual(diff_text.returncode, 0, diff_text.stderr + diff_text.stdout)
            self.assertIn("LoopPlane task diff", diff_text.stdout)
            self.assertIn("Diff metadata: available", diff_text.stdout)
            self.assertIn("Changed files: 2", diff_text.stdout)
            self.assertIn("src/app.py (modified, +2 -1)", diff_text.stdout)
            self.assertIn("tests/test_app.py (added, +2 -0)", diff_text.stdout)
            self.assertIn(
                f"Patch artifact: {paths.value('results_dir')}/T900/runs/run-diff/git/project_diff.patch",
                diff_text.stdout,
            )
            self.assertIn(f"Before-run checkpoint: {pre['metadata']['checkpoint']['checkpoint_id']}", diff_text.stdout)
            self.assertIn(f"After-run checkpoint: {after_payload['checkpoint']['checkpoint_id']}", diff_text.stdout)
            self.assertNotIn(".git", diff_text.stdout)
            self.assertNotIn("not implemented", diff_text.stdout)

    def test_cli_diff_reports_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Missing diff task.")
            self._write_single_task_plan(project, task_id="T001")

            result = self.run_loopplane("vc", "diff", "--project", str(project), "--task", "T404", "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "task_not_found")
            self.assertIn("Task 'T404' was not found", "\n".join(payload["errors"]))
            self.assertNotIn(".git", result.stdout)

    def test_cli_diff_reports_existing_task_without_diff_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No diff metadata.")
            self._write_single_task_plan(project, task_id="T777")
            run_dir = project / ".loopplane" / "results" / "T777" / "runs" / "run-no-diff"
            run_dir.mkdir(parents=True)
            self._write_latest(project, task_id="T777", run_id="run-no-diff", run_dir=run_dir)

            result = self.run_loopplane("vc", "diff", "--project", str(project), "--task", "T777", "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "unavailable")
            self.assertFalse(payload["diff"]["available"])
            self.assertIn("No captured diff metadata", payload["message"])
            self.assertEqual(payload["runs_considered"][0]["run_id"], "run-no-diff")
            self.assertFalse(payload["runs_considered"][0]["diff_metadata_found"])
            self.assertNotIn(".git", result.stdout)

            text = self.run_loopplane("vc", "diff", "--project", str(project), "--task", "T777")
            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("Diff metadata: unavailable", text.stdout)
            self.assertIn("run-no-diff", text.stdout)
            self.assertNotIn(".git", text.stdout)

    def test_cli_log_reports_checkpoint_records_text_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Checkpoint log surface.")
            self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)

            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(project, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            add_initial = self.run_git(project, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(project, "commit", "-m", "initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

            first = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "manual_checkpoint",
                "--task",
                "T901",
                "--run",
                "run-first",
                "--json",
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            first_payload = json.loads(first.stdout)
            (project / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
            second = self.run_loopplane(
                "vc",
                "checkpoint",
                "--project",
                str(project),
                "--reason",
                "after_validation_pass",
                "--task",
                "T901",
                "--run",
                "run-second",
                "--json",
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            second_payload = json.loads(second.stdout)

            log_json = self.run_loopplane("vc", "log", "--project", str(project), "--json")
            self.assertEqual(log_json.returncode, 0, log_json.stderr + log_json.stdout)
            self.assertNotIn(".git", log_json.stdout)
            payload = json.loads(log_json.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["checkpoint_count"], 2)
            self.assertEqual(payload["returned_count"], 2)
            self.assertEqual(payload["order"], "newest_first")
            self.assertFalse(payload["sources"]["direct_git_reads"])
            self.assertEqual(
                payload["sources"]["checkpoint_log"],
                f"{self.workflow_paths(project).value('runtime_dir')}/git_checkpoints.jsonl",
            )
            self.assertEqual(payload["checkpoints"][0]["checkpoint_id"], second_payload["checkpoint"]["checkpoint_id"])
            self.assertEqual(payload["checkpoints"][1]["checkpoint_id"], first_payload["checkpoint"]["checkpoint_id"])
            self.assertEqual(payload["checkpoints"][0]["reason"], "after_validation_pass")
            self.assertEqual(payload["checkpoints"][0]["task_id"], "T901")
            self.assertEqual(payload["checkpoints"][0]["run_id"], "run-second")
            for checkpoint in payload["checkpoints"]:
                self.assertNotIn("ref", checkpoint)
                self.assertNotIn("repository_root", checkpoint)

            limited = self.run_loopplane("vc", "log", "--project", str(project), "--limit", "1", "--json")
            self.assertEqual(limited.returncode, 0, limited.stderr + limited.stdout)
            limited_payload = json.loads(limited.stdout)
            self.assertEqual(limited_payload["checkpoint_count"], 2)
            self.assertEqual(limited_payload["returned_count"], 1)
            self.assertEqual(limited_payload["limit"], 1)
            self.assertEqual(
                limited_payload["checkpoints"][0]["checkpoint_id"],
                second_payload["checkpoint"]["checkpoint_id"],
            )

            log_text = self.run_loopplane("vc", "log", "--project", str(project))
            self.assertEqual(log_text.returncode, 0, log_text.stderr + log_text.stdout)
            self.assertIn("LoopPlane checkpoint log", log_text.stdout)
            self.assertIn("Checkpoints: 2", log_text.stdout)
            self.assertIn(second_payload["checkpoint"]["checkpoint_id"], log_text.stdout)
            self.assertIn("after_validation_pass", log_text.stdout)
            self.assertIn("task T901", log_text.stdout)
            self.assertIn("Rollback action: none", log_text.stdout)
            self.assertNotIn("not implemented", log_text.stdout)
            self.assertNotIn(".git", log_text.stdout)

    def test_cli_log_reports_no_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No checkpoint records.")

            log_json = self.run_loopplane("vc", "log", "--project", str(project), "--json")
            self.assertEqual(log_json.returncode, 0, log_json.stderr + log_json.stdout)
            payload = json.loads(log_json.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "empty")
            self.assertEqual(payload["checkpoint_count"], 0)
            self.assertEqual(payload["checkpoints"], [])
            self.assertEqual(payload["message"], "No checkpoints recorded.")
            self.assertNotIn(".git", log_json.stdout)

            log_text = self.run_loopplane("vc", "log", "--project", str(project))
            self.assertEqual(log_text.returncode, 0, log_text.stderr + log_text.stdout)
            self.assertIn("No checkpoints recorded.", log_text.stdout)
            self.assertNotIn("not implemented", log_text.stdout)

    def test_cli_log_reports_missing_checkpoint_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Missing checkpoint log.")
            (project / ".loopplane" / "runtime" / "git_checkpoints.jsonl").unlink()

            result = self.run_loopplane("vc", "log", "--project", str(project), "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "empty")
            self.assertFalse(payload["sources"]["checkpoint_log_loaded"])
            self.assertIn("Checkpoint log is missing", "\n".join(payload["warnings"]))
            self.assertNotIn(".git", result.stdout)

    def test_cli_log_reports_malformed_and_unsafe_checkpoint_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Malformed checkpoint log.")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            checkpoint_log = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
            checkpoint_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema_version": "1.5",
                                "workflow_id": workflow["workflow_id"],
                                "checkpoint_id": "cp_safe",
                                "created_at": "2026-06-11T00:00:00Z",
                                "reason": "manual_checkpoint",
                                "status": "created",
                                "provider": "git",
                                "backend": "managed_refs",
                                "ref": "refs/loopplane/wf/checkpoints/cp_safe",
                                "repository_root": str(project / ".git"),
                                "commit": "abc123def456",
                            },
                            sort_keys=True,
                        ),
                        "{not json with .git/config",
                        json.dumps(
                            {
                                "schema_version": "1.5",
                                "workflow_id": workflow["workflow_id"],
                                "checkpoint_id": ".git/leak",
                                "created_at": "2026-06-11T00:01:00Z",
                                "reason": "manual_checkpoint",
                                "status": "created",
                            },
                            sort_keys=True,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane("vc", "log", "--project", str(project), "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertNotIn(".git", result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "invalid_metadata")
            self.assertEqual(payload["checkpoint_count"], 1)
            self.assertEqual(payload["checkpoints"][0]["checkpoint_id"], "cp_safe")
            warnings = "\n".join(payload["warnings"])
            self.assertIn("malformed JSON", warnings)
            self.assertIn("unsafe repository_root field", warnings)
            self.assertIn("missing a safe checkpoint_id", warnings)
            self.assertNotIn("ref", payload["checkpoints"][0])

    def test_cli_log_reports_disabled_version_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Disabled version control.")
            config_path = self.workflow_paths(project).version_control_config_file
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["enabled"] = False
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = self.run_loopplane("vc", "log", "--project", str(project), "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["enabled"])
            self.assertEqual(payload["status"], "disabled")
            self.assertNotIn(".git", result.stdout)

    def test_cli_log_reports_version_control_unavailable_from_read_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Unavailable version control.", git_runner=FakeGitRunner(available=False))

            result = self.run_loopplane("vc", "log", "--project", str(project), "--json")

            self.assertEqual(result.returncode, 13, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "waiting_config")
            self.assertFalse(payload["git_available"])
            self.assertIn("Git is unavailable", "\n".join(payload["errors"]))
            self.assertNotIn(".git", result.stdout)

    def test_cli_log_reports_missing_workflow_and_invalid_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            missing = self.run_loopplane("vc", "log", "--project", str(project), "--json")
            self.assertEqual(missing.returncode, 2, missing.stderr + missing.stdout)
            missing_payload = json.loads(missing.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "waiting_config")
            self.assertIn("Missing .loopplane/config/workflow.json", "\n".join(missing_payload["errors"]))

            init_project(project, "Invalid log limit.")
            invalid_limit = self.run_loopplane("vc", "log", "--project", str(project), "--limit", "0", "--json")
            self.assertEqual(invalid_limit.returncode, 2, invalid_limit.stderr + invalid_limit.stdout)
            invalid_payload = json.loads(invalid_limit.stdout)
            self.assertFalse(invalid_payload["ok"])
            self.assertIn("limit must be a positive integer", "\n".join(invalid_payload["errors"]))
            self.assertNotIn("unrecognized arguments", invalid_limit.stderr)

    def test_cli_export_writes_git_bundle_with_only_loopplane_managed_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, checkpoint_id = self._project_with_checkpoint(Path(tmp), dirty=False)
            bundle = Path(tmp) / "loopplane_git_refs.bundle"
            text_bundle = Path(tmp) / "loopplane_git_refs_text.bundle"
            paths = self.workflow_paths(project)
            checkpoint_records = self._read_jsonl(paths.runtime_dir / "git_checkpoints.jsonl")
            checkpoint_ref = str(checkpoint_records[-1]["ref"])

            user_branch = self.run_git(project, "branch", "user-feature")
            self.assertEqual(user_branch.returncode, 0, user_branch.stderr + user_branch.stdout)
            branch_before = self.run_git(project, "branch", "--show-current").stdout.strip()
            head_before = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
            index_before = self.run_git(project, "ls-files", "--stage", "-z").stdout
            status_before = self.run_git(project, "status", "--short").stdout.splitlines()

            result = self.run_loopplane(
                "vc",
                "export",
                "--project",
                str(project),
                "--output",
                str(bundle),
                "--json",
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(bundle.is_file())
            self.assertNotIn("not implemented", result.stdout)
            self.assertNotIn(".git", result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "exported")
            self.assertEqual(payload["schema_version"], "loopplane-git-ref-bundle-export-1")
            self.assertEqual(payload["bundle"]["format"], "git_bundle")
            self.assertEqual(payload["bundle"]["refs"], [checkpoint_ref])
            self.assertEqual(payload["bundle"]["ref_count"], 1)
            self.assertTrue(payload["bundle"]["managed_refs_only"])
            self.assertEqual(payload["checkpoint_log"]["record_count"], 1)
            self.assertTrue(payload["safety"]["active_branch_unchanged"])
            self.assertTrue(payload["safety"]["head_unchanged"])
            self.assertTrue(payload["safety"]["user_index_unchanged"])
            self.assertFalse(payload["safety"]["remote_operations_performed"])
            self.assertFalse(payload["safety"]["history_rewritten"])

            verify = self.run_git(project, "bundle", "verify", str(bundle))
            self.assertEqual(verify.returncode, 0, verify.stderr + verify.stdout)
            heads = self.run_git(project, "bundle", "list-heads", str(bundle))
            self.assertEqual(heads.returncode, 0, heads.stderr + heads.stdout)
            head_refs = [line.split(maxsplit=1)[1] for line in heads.stdout.splitlines() if line.strip()]
            self.assertEqual(head_refs, [checkpoint_ref])
            self.assertTrue(all(ref.startswith("refs/loopplane/") for ref in head_refs))
            self.assertFalse(any("refs/heads/user-feature" in ref for ref in head_refs))
            self.assertEqual(payload["bundle"]["heads"], [{"commit": heads.stdout.split()[0], "ref": checkpoint_ref}])

            branch_after = self.run_git(project, "branch", "--show-current").stdout.strip()
            head_after = self.run_git(project, "rev-parse", "HEAD").stdout.strip()
            index_after = self.run_git(project, "ls-files", "--stage", "-z").stdout
            status_after = self.run_git(project, "status", "--short").stdout.splitlines()
            self.assertEqual(branch_before, branch_after)
            self.assertEqual(head_before, head_after)
            self.assertEqual(index_before, index_after)
            self.assertEqual(status_before, status_after)

            text = self.run_loopplane(
                "vc",
                "export",
                "--project",
                str(project),
                "--output",
                str(text_bundle),
            )
            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("LoopPlane Git checkpoint bundle export", text.stdout)
            self.assertIn("Status: exported", text.stdout)
            self.assertIn("Managed refs only: yes", text.stdout)
            self.assertIn("Active branch unchanged: yes", text.stdout)
            self.assertIn(checkpoint_id, text.stdout)
            self.assertNotIn("not implemented", text.stdout)
            self.assertNotIn(".git", text.stdout)

    def test_cli_export_reports_no_checkpoint_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No checkpoint refs to export.")
            bundle = Path(tmp) / "empty.bundle"

            result = self.run_loopplane("vc", "export", "--project", str(project), "--output", str(bundle), "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertFalse(bundle.exists())
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "no_checkpoints")
            self.assertEqual(payload["bundle"]["ref_count"], 0)
            self.assertIn("No LoopPlane-managed checkpoint refs", "\n".join(payload["errors"]))
            self.assertNotIn(".git", result.stdout)

    def test_cli_import_restores_only_loopplane_refs_and_preserves_user_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, checkpoint_id = self._project_with_checkpoint(root / "source", dirty=False)
            paths = self.workflow_paths(source)
            checkpoint_ref = str(self._read_jsonl(paths.runtime_dir / "git_checkpoints.jsonl")[-1]["ref"])
            source_user_branch = self.run_git(source, "branch", "bundled-user-branch")
            self.assertEqual(source_user_branch.returncode, 0, source_user_branch.stderr + source_user_branch.stdout)
            mixed_bundle = root / "mixed_refs.bundle"
            create_bundle = self.run_git(
                source,
                "bundle",
                "create",
                str(mixed_bundle),
                checkpoint_ref,
                "refs/heads/bundled-user-branch",
            )
            self.assertEqual(create_bundle.returncode, 0, create_bundle.stderr + create_bundle.stdout)
            bundle_heads = self.run_git(source, "bundle", "list-heads", str(mixed_bundle))
            self.assertEqual(bundle_heads.returncode, 0, bundle_heads.stderr + bundle_heads.stdout)
            checkpoint_commit = next(
                line.split()[0]
                for line in bundle_heads.stdout.splitlines()
                if line.strip().endswith(checkpoint_ref)
            )

            target = root / "target"
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
            git_init = self.run_git(target, "init")
            self.assertEqual(git_init.returncode, 0, git_init.stderr + git_init.stdout)
            for key, value in (
                ("user.name", "LoopPlane Test"),
                ("user.email", "loopplane-test@example.invalid"),
            ):
                config = self.run_git(target, "config", key, value)
                self.assertEqual(config.returncode, 0, config.stderr + config.stdout)
            add_initial = self.run_git(target, "add", ".")
            self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
            commit_initial = self.run_git(target, "commit", "-m", "target initial")
            self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)
            target_user_branch = self.run_git(target, "branch", "target-user-branch")
            self.assertEqual(target_user_branch.returncode, 0, target_user_branch.stderr + target_user_branch.stdout)

            branch_before = self.run_git(target, "branch", "--show-current").stdout.strip()
            head_before = self.run_git(target, "rev-parse", "HEAD").stdout.strip()
            index_before = self.run_git(target, "ls-files", "--stage", "-z").stdout
            status_before = self.run_git(target, "status", "--short").stdout.splitlines()

            result = self.run_loopplane("vc", "import", str(mixed_bundle), "--project", str(target), "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn("not implemented", result.stdout)
            self.assertNotIn(".git", result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "imported")
            self.assertEqual(payload["schema_version"], "loopplane-git-ref-bundle-import-1")
            self.assertEqual(payload["bundle"]["importable_refs"], [checkpoint_ref])
            self.assertEqual(payload["bundle"]["ignored_refs"], ["refs/heads/bundled-user-branch"])
            self.assertFalse(payload["bundle"]["managed_refs_only"])
            self.assertTrue(payload["import"]["managed_refs_only"])
            self.assertEqual(payload["import"]["imported_count"], 1)
            self.assertEqual(payload["import"]["refs"][0]["ref"], checkpoint_ref)
            self.assertEqual(payload["import"]["refs"][0]["commit"], checkpoint_commit)
            self.assertEqual(payload["import"]["refs"][0]["action"], "created")
            self.assertTrue(payload["safety"]["active_branch_unchanged"])
            self.assertTrue(payload["safety"]["head_unchanged"])
            self.assertTrue(payload["safety"]["user_index_unchanged"])
            self.assertFalse(payload["safety"]["remote_operations_performed"])
            self.assertFalse(payload["safety"]["history_rewritten"])
            self.assertFalse(payload["safety"]["user_branch_modified"])
            self.assertIn("Ignoring non-LoopPlane-managed refs", "\n".join(payload["warnings"]))

            imported_ref = self.run_git(target, "show-ref", "--verify", checkpoint_ref)
            self.assertEqual(imported_ref.returncode, 0, imported_ref.stderr + imported_ref.stdout)
            self.assertEqual(imported_ref.stdout.split()[0], checkpoint_commit)
            imported_user_branch = self.run_git(target, "show-ref", "--verify", "refs/heads/bundled-user-branch")
            self.assertNotEqual(imported_user_branch.returncode, 0)
            preserved_user_branch = self.run_git(target, "show-ref", "--verify", "refs/heads/target-user-branch")
            self.assertEqual(preserved_user_branch.returncode, 0, preserved_user_branch.stderr + preserved_user_branch.stdout)

            branch_after = self.run_git(target, "branch", "--show-current").stdout.strip()
            head_after = self.run_git(target, "rev-parse", "HEAD").stdout.strip()
            index_after = self.run_git(target, "ls-files", "--stage", "-z").stdout
            status_after = self.run_git(target, "status", "--short").stdout.splitlines()
            self.assertEqual(branch_before, branch_after)
            self.assertEqual(head_before, head_after)
            self.assertEqual(index_before, index_after)
            self.assertEqual(status_before, status_after)

            text = self.run_loopplane("vc", "import", str(mixed_bundle), "--project", str(target))
            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("LoopPlane Git checkpoint bundle import", text.stdout)
            self.assertIn("Status: imported", text.stdout)
            self.assertIn("Imported refs managed only: yes", text.stdout)
            self.assertIn("Active branch unchanged: yes", text.stdout)
            self.assertIn(checkpoint_id, text.stdout)
            self.assertNotIn("not implemented", text.stdout)
            self.assertNotIn(".git", text.stdout)

    def test_cli_import_rejects_bundle_without_loopplane_managed_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _checkpoint_id = self._project_with_checkpoint(root / "source", dirty=False)
            branch_only_bundle = root / "branch_only.bundle"
            create_bundle = self.run_git(source, "bundle", "create", str(branch_only_bundle), "HEAD")
            self.assertEqual(create_bundle.returncode, 0, create_bundle.stderr + create_bundle.stdout)

            target = root / "target"
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
            git_init = self.run_git(target, "init")
            self.assertEqual(git_init.returncode, 0, git_init.stderr + git_init.stdout)
            branch_before = self.run_git(target, "branch", "--show-current").stdout.strip()
            head_before = self.run_git(target, "rev-parse", "--verify", "HEAD")
            index_before = self.run_git(target, "ls-files", "--stage", "-z").stdout

            result = self.run_loopplane("vc", "import", str(branch_only_bundle), "--project", str(target), "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "no_checkpoints")
            self.assertEqual(payload["bundle"]["importable_ref_count"], 0)
            self.assertEqual(payload["import"]["imported_count"], 0)
            self.assertIn("No LoopPlane-managed checkpoint refs", "\n".join(payload["errors"]))
            self.assertNotIn(".git", result.stdout)
            branch_after = self.run_git(target, "branch", "--show-current").stdout.strip()
            head_after = self.run_git(target, "rev-parse", "--verify", "HEAD")
            index_after = self.run_git(target, "ls-files", "--stage", "-z").stdout
            self.assertEqual(branch_before, branch_after)
            self.assertEqual(head_before.returncode, head_after.returncode)
            self.assertEqual(head_before.stdout, head_after.stdout)
            self.assertEqual(index_before, index_after)

    def test_cli_rollback_executes_json_request_without_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, checkpoint_id = self._project_with_checkpoint(Path(tmp), approval_enabled=True)

            result = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", checkpoint_id, "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertNotIn(".git", result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "executed")
            self.assertEqual(payload["checkpoint_id"], checkpoint_id)
            self.assertEqual(payload["target_checkpoint"]["checkpoint_id"], checkpoint_id)
            self.assertNotIn("ref", payload["target_checkpoint"])
            self.assertFalse(payload["approval_required"])
            self.assertTrue(payload["approval_policy"]["enabled"])
            self.assertTrue(payload["execution"]["performed"])
            self.assertTrue(payload["execution"]["worktree_mutated"])
            self.assertFalse(payload["execution"]["history_rewritten"])
            self.assertTrue(payload["execution"]["user_branch_preserved"])
            self.assertTrue(payload["execution"]["user_index_preserved"])
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertFalse((project / "tests" / "test_app.py").exists())
            paths = self.workflow_paths(project)
            self.assertEqual(payload["sources"]["checkpoint_log"], f"{paths.value('runtime_dir')}/git_checkpoints.jsonl")
            self.assertEqual(
                payload["sources"]["version_control_status"],
                f"{paths.value('read_models_dir')}/version_control_status.json",
            )
            self.assertEqual(
                payload["sources"]["rollback_requests"],
                f"{paths.value('requests_dir')}/version_control_rollback_requests.jsonl",
            )
            self.assertTrue(payload["sources"]["mutation_performed"])
            self.assertTrue(payload["sources"]["managed_checkpoint_metadata"])
            affected = {entry["path"]: entry for entry in payload["affected_paths"]}
            self.assertIn("src/app.py", affected)
            self.assertIn("tests/test_app.py", affected)
            self.assertTrue(payload["risk_summary"]["dirty_worktree"])
            self.assertGreaterEqual(payload["risk_summary"]["dirty_files_count"], 2)
            self.assertGreaterEqual(payload["risk_summary"]["affected_paths_count"], 2)
            self.assertFalse(payload["risk_summary"]["history_rewrite_before_approval"])
            self.assertFalse(payload["risk_summary"]["worktree_mutation_before_approval"])

            rollback_records = self._read_jsonl(paths.requests_dir / "version_control_rollback_requests.jsonl")
            approval_records = self._read_jsonl(paths.runtime_dir / "human_approval_requests.jsonl")
            self.assertEqual(len(rollback_records), 1)
            self.assertEqual(approval_records, [])
            self.assertEqual(rollback_records[0]["checkpoint_id"], checkpoint_id)
            self.assertEqual(rollback_records[0]["status"], "executed")
            self.assertFalse(rollback_records[0]["approval_required"])

    def test_cli_rollback_executes_text_request_without_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, checkpoint_id = self._project_with_checkpoint(Path(tmp), approval_enabled=True)

            result = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", checkpoint_id)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("LoopPlane rollback request", result.stdout)
            self.assertIn("Status: executed", result.stdout)
            self.assertIn(f"Target checkpoint: {checkpoint_id}", result.stdout)
            self.assertIn("Approval required: no", result.stdout)
            self.assertIn("Execution performed: yes", result.stdout)
            self.assertIn("Affected paths:", result.stdout)
            self.assertIn("src/app.py", result.stdout)
            self.assertIn("tests/test_app.py", result.stdout)
            self.assertIn("history rewrite before approval: no", result.stdout)
            self.assertIn("user worktree mutation before approval: no", result.stdout)
            self.assertIn(
                f"{self.workflow_paths(project).value('requests_dir')}/version_control_rollback_requests.jsonl",
                result.stdout,
            )
            self.assertNotIn(".git", result.stdout)
            self.assertNotIn("not implemented", result.stdout)

    def test_cli_rollback_reports_unknown_checkpoint_without_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _checkpoint_id = self._project_with_checkpoint(Path(tmp), dirty=False)

            result = self.run_loopplane(
                "vc",
                "rollback",
                "--project",
                str(project),
                "--checkpoint",
                "missing-checkpoint",
                "--json",
            )

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "checkpoint_not_found")
            self.assertIn("was not found", "\n".join(payload["errors"]))
            self.assertIsNone(payload["rollback_request"])
            self.assertEqual(self._read_jsonl(project / ".loopplane" / "requests" / "version_control_rollback_requests.jsonl"), [])
            self.assertNotIn(".git", result.stdout)

    def test_cli_rollback_reports_no_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Rollback no checkpoints.")

            result = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", "cp_missing", "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "no_checkpoints")
            self.assertIn("No checkpoints are recorded", "\n".join(payload["errors"]))
            self.assertNotIn(".git", result.stdout)

    def test_cli_rollback_reports_malformed_checkpoint_records_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Rollback malformed records.")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            checkpoint_log = project / ".loopplane" / "runtime" / "git_checkpoints.jsonl"
            checkpoint_log.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "schema_version": "1.5",
                                "workflow_id": workflow["workflow_id"],
                                "checkpoint_id": "cp_safe",
                                "created_at": "2026-06-11T00:00:00Z",
                                "reason": "manual_checkpoint",
                                "status": "created",
                                "provider": "git",
                                "backend": "managed_refs",
                                "ref": "refs/loopplane/wf/checkpoints/cp_safe",
                                "repository_root": str(project / ".git"),
                                "commit": "abc123def456",
                            },
                            sort_keys=True,
                        ),
                        "{not json with .git/config",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", "cp_safe", "--json")

            self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
            self.assertNotIn(".git", result.stdout)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "invalid_metadata")
            self.assertIn("Checkpoint metadata is invalid", "\n".join(payload["errors"]))
            warnings = "\n".join(payload["warnings"])
            self.assertIn("malformed JSON", warnings)
            self.assertIn("unsafe repository_root field", warnings)
            self.assertIsNone(payload["rollback_request"])

    def test_cli_rollback_reports_disabled_and_unavailable_version_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, checkpoint_id = self._project_with_checkpoint(Path(tmp), dirty=False)
            config_path = self.workflow_paths(project).version_control_config_file
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["enabled"] = False
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            disabled = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", checkpoint_id, "--json")

            self.assertEqual(disabled.returncode, 13, disabled.stderr + disabled.stdout)
            disabled_payload = json.loads(disabled.stdout)
            self.assertFalse(disabled_payload["ok"])
            self.assertEqual(disabled_payload["status"], "disabled")
            self.assertIn("Version control is disabled", "\n".join(disabled_payload["errors"]))
            self.assertNotIn(".git", disabled.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            project, checkpoint_id = self._project_with_checkpoint(Path(tmp), dirty=False)
            env = dict(os.environ)
            env["PATH"] = ""

            unavailable = self.run_loopplane(
                "vc",
                "rollback",
                "--project",
                str(project),
                "--checkpoint",
                checkpoint_id,
                "--json",
                env=env,
            )

            self.assertEqual(unavailable.returncode, 13, unavailable.stderr + unavailable.stdout)
            unavailable_payload = json.loads(unavailable.stdout)
            self.assertFalse(unavailable_payload["ok"])
            self.assertEqual(unavailable_payload["status"], "waiting_config")
            self.assertIn("Git is unavailable", "\n".join(unavailable_payload["errors"]))
            self.assertNotIn(".git", unavailable.stdout)

    def test_cli_rollback_reports_missing_workflow_and_parser_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            missing = self.run_loopplane("vc", "rollback", "--project", str(project), "--checkpoint", "cp_missing", "--json")
            self.assertEqual(missing.returncode, 2, missing.stderr + missing.stdout)
            missing_payload = json.loads(missing.stdout)
            self.assertFalse(missing_payload["ok"])
            self.assertEqual(missing_payload["status"], "waiting_config")
            self.assertIn("Missing .loopplane/config/workflow.json", "\n".join(missing_payload["errors"]))

            parser_error = self.run_loopplane("vc", "rollback", "--project", str(project), "--json")
            self.assertEqual(parser_error.returncode, 2)
            self.assertIn("--checkpoint", parser_error.stderr)

    def _status_for_paths(self, lines: list[str], paths: tuple[str, ...]) -> list[str]:
        return sorted(line for line in lines if any(line.endswith(path) for path in paths))

    def _porcelain_z_count(self, project: Path) -> int:
        status = self.run_git(project, "status", "--porcelain=v1", "-z")
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        return sum(1 for entry in status.stdout.split("\0") if entry)

    def _project_with_checkpoint(
        self,
        tmp: Path,
        *,
        dirty: bool = True,
        approval_enabled: bool = False,
    ) -> tuple[Path, str]:
        project = tmp / "project"
        init_result = self.run_loopplane("init", "--project", str(project), "--brief", "Rollback fixture.")
        self.assertEqual(init_result.returncode, 0, init_result.stderr + init_result.stdout)
        paths = self.workflow_paths(project)
        if approval_enabled:
            security_path = paths.config_file("security.json")
            security = json.loads(security_path.read_text(encoding="utf-8"))
            security["approval"]["enabled"] = True
            security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        for key, value in (
            ("user.name", "LoopPlane Test"),
            ("user.email", "loopplane-test@example.invalid"),
        ):
            config = self.run_git(project, "config", key, value)
            self.assertEqual(config.returncode, 0, config.stderr + config.stdout)

        (project / "src").mkdir()
        (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        add_initial = self.run_git(project, "add", ".")
        self.assertEqual(add_initial.returncode, 0, add_initial.stderr + add_initial.stdout)
        commit_initial = self.run_git(project, "commit", "-m", "initial")
        self.assertEqual(commit_initial.returncode, 0, commit_initial.stderr + commit_initial.stdout)

        checkpoint = self.run_loopplane(
            "vc",
            "checkpoint",
            "--project",
            str(project),
            "--reason",
            "manual_checkpoint",
            "--json",
        )
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr + checkpoint.stdout)
        checkpoint_id = json.loads(checkpoint.stdout)["checkpoint"]["checkpoint_id"]

        if dirty:
            (project / "src" / "app.py").write_text("VALUE = 2\nEXTRA = 3\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "def test_app():\n    assert True\n",
                encoding="utf-8",
            )
        return project, checkpoint_id

    def _read_jsonl(self, path: Path) -> list[dict[str, object]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def _write_single_task_plan(self, project: Path, *, task_id: str) -> None:
        paths = self.workflow_paths(project)
        workflow = json.loads(paths.workflow_config_file.read_text(encoding="utf-8"))
        plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Diff Fixture

- [ ] {task_id}: Exercise task diff metadata
  - acceptance: Task diff metadata is shown.
  - evidence: {paths.value("results_dir")}/{task_id}/
  - latest: {paths.value("results_dir")}/{task_id}/latest.json
  - depends_on: []
  - risk: low
  - validation: Diff metadata validation.
  - max_attempts: 1
  - approval: not_required
  - deliverables: Diff metadata output.
"""
        paths.plan_file.write_text(plan, encoding="utf-8")

    def _write_latest(self, project: Path, *, task_id: str, run_id: str, run_dir: Path) -> None:
        paths = self.workflow_paths(project)
        latest = {
            "schema_version": "1.5",
            "task_id": task_id,
            "latest_run_id": run_id,
            "latest_run_dir": run_dir.relative_to(project).as_posix(),
            "validation_path": f"{paths.value('results_dir')}/{task_id}/runs/{run_id}/validation.json",
            "validation_status": "pass",
            "updated_at": "2026-06-11T00:00:00Z",
            "updated_by": "test",
        }
        latest_path = paths.results_dir / task_id / "latest.json"
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_text(json.dumps(latest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
