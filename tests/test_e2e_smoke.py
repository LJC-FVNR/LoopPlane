from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.plan_objectives import objective_structure_fingerprint, parse_plan_objectives
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from tests.test_objective_gates import configure_fake_objective_verifier


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
CLI_ADAPTER_FIXTURE_BIN = REPO_ROOT / "tests" / "fixtures" / "cli_adapters" / "bin"


def run_json(*args: str, expect: int = 0, env: dict[str, str] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def install_cli_adapter_fixture_bin(root: Path) -> Path:
    bin_dir = root / "fixture-bin"
    bin_dir.mkdir()
    target = bin_dir / "codex"
    shutil.copy2(CLI_ADAPTER_FIXTURE_BIN / "codex", target)
    target.chmod(target.stat().st_mode | 0o111)
    return bin_dir


def workflow_paths(project: Path) -> WorkflowPaths:
    return WorkflowPaths.from_config(project, load_workflow_config(project))


def configure_runner(project: Path, runner_id: str, **updates: Any) -> None:
    config_path = workflow_paths(project).config_file("agent_runners.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"][runner_id].update(updates)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_runner_enabled(project: Path, runner_id: str, enabled: bool) -> None:
    config_path = workflow_paths(project).config_file("agent_runners.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if runner_id in config["runners"]:
        config["runners"][runner_id]["enabled"] = enabled
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def require_auditor(project: Path) -> None:
    paths = workflow_paths(project)
    workflow_path = paths.workflow_config_file
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["planning"]["auditor_required"] = True
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_shell_smoke_plan(project: Path) -> None:
    paths = workflow_paths(project)
    workflow = json.loads(paths.workflow_config_file.read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Shell Smoke

- [ ] T001: Run Python shell worker
  - acceptance: Shell worker writes result artifact.
  - acceptance: Worker report records shell completion.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Shell worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.

## Final Objective Checklist

- [ ] `FO1` Shell smoke workflow reaches completion with expected artifacts.
  - evidence_scope: {paths.value("results_dir")}/T001/
  - judgment_guidance: Confirm the shell smoke worker produced the expected artifact.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
    paths.plan_file.write_text(plan, encoding="utf-8")
    write_final_objective_report(project)


def write_codex_smoke_plan(project: Path) -> None:
    paths = workflow_paths(project)
    workflow = json.loads(paths.workflow_config_file.read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Codex Worker Smoke

- [ ] P0.T001: Run Codex CLI worker fixture
  - acceptance: Fake Codex worker writes result artifact.
  - acceptance: Worker report records Codex completion.
  - evidence: {paths.value("results_dir")}/P0.T001/
  - latest: {paths.value("results_dir")}/P0.T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Fake Codex worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.

## Final Objective Checklist

- [ ] `FO1` Codex fixture workflow reaches completion with expected artifacts.
  - evidence_scope: {paths.value("results_dir")}/P0.T001/
  - judgment_guidance: Confirm the Codex fixture worker produced the expected artifact.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
    paths.plan_file.write_text(plan, encoding="utf-8")
    write_final_objective_report(project)


def write_final_objective_report(project: Path) -> None:
    paths = workflow_paths(project)
    workflow = json.loads(paths.workflow_config_file.read_text(encoding="utf-8"))
    plan_text = paths.plan_file.read_text(encoding="utf-8")
    objectives, _errors = parse_plan_objectives(plan_text)
    workflow_objectives = [objective for objective in objectives if objective.scope == "workflow"]
    report_path = paths.runtime_dir / "objectives" / "final_objective_verification.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "workflow_id": workflow["workflow_id"],
                "scope": "workflow",
                "phase_id": None,
                "phase_title": None,
                "status": "satisfied",
                "verified_at": "2026-06-10T00:00:00Z",
                "plan_sha256": "sha256:" + sha256(plan_text.encode("utf-8")).hexdigest(),
                "objective_structure_fingerprint": objective_structure_fingerprint(
                    plan_text,
                    objectives=workflow_objectives,
                ),
                "objective_results": [
                    {
                        "objective_id": "FO1",
                        "status": "satisfied",
                        "verdict": "satisfied",
                        "confidence": "high",
                        "evidence_reviewed": [paths.value("results_dir")],
                        "agent_rationale": "Smoke objective is pre-satisfied by the fixture.",
                        "expandable": False,
                    }
                ],
                "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def write_shell_worker(project: Path) -> Path:
    script = project / "worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path

            run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
            (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
            (run_dir / "logs").mkdir(parents=True, exist_ok=True)
            (run_dir / "raw").mkdir(parents=True, exist_ok=True)
            (run_dir / "artifacts" / "result.txt").write_text("shell smoke result\\n", encoding="utf-8")
            (run_dir / "report.md").write_text("# Worker Report\\n\\nShell worker completed.\\n", encoding="utf-8")
            (run_dir / "commands.sh").write_text("python worker.py\\n", encoding="utf-8")
            status = {
                "schema_version": "1.5",
                "run_id": os.environ["LOOPPLANE_RUN_ID"],
                "task_id": os.environ["LOOPPLANE_TASK_ID"],
                "primary_task_id": os.environ["LOOPPLANE_TASK_ID"],
                "phase": "Phase P0: Shell Smoke",
                "status": "completed",
                "next_prompt_ready": True,
                "project_changes": [],
                "commands_run": [{"cmd": "python worker.py", "exit_code": 0}],
                "key_outputs": [str(run_dir / "artifacts" / "result.txt")],
                "evidence_satisfies": [
                    {
                        "task_id": os.environ["LOOPPLANE_TASK_ID"],
                        "relationship": "primary",
                        "acceptance_claimed": [
                            "Shell worker writes result artifact.",
                            "Worker report records shell completion.",
                        ],
                        "evidence": [
                            str(run_dir / "artifacts" / "result.txt"),
                            str(run_dir / "report.md"),
                        ],
                    }
                ],
                "validation_claim": {
                    "claim": "completed",
                    "checks_claimed": [{"name": "shell_smoke", "status": "pass"}],
                    "limitations": [],
                },
                "summary_candidate": {
                    "one_line": "Shell smoke worker completed.",
                    "highlights": ["result artifact written"],
                    "warnings": [],
                    "blockers": [],
                },
                "background": {
                    "pids": [],
                    "commands": [],
                    "logs": [],
                    "heartbeat_required": False,
                    "wake_next_agent_when": None,
                },
                "repair_attempts": [],
                "known_risks": [],
                "remaining_incomplete_items": [],
            }
            (run_dir / "agent_status.json").write_text(
                json.dumps(status, indent=2, sort_keys=True) + "\\n",
                encoding="utf-8",
            )
            print("shell smoke worker completed")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


class EndToEndSmokeTest(unittest.TestCase):
    def test_minimal_noop_project_plans_audits_and_activates_through_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "init",
                    "--project",
                    str(project),
                    "--brief",
                    "Minimal noop smoke.",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            configure_runner(project, "planner", adapter="noop", command="noop", enabled=True)
            configure_runner(project, "auditor", adapter="noop", command="noop", enabled=True)
            require_auditor(project)

            plan = run_json("plan", "--project", str(project), "--json")
            audit = run_json("audit-plan", "--project", str(project), "--json")
            activate = run_json("activate-plan", "--project", str(project), "--json")

            self.assertEqual(plan["status"], "ready_for_audit")
            self.assertEqual(audit["status"], "passed")
            self.assertEqual(activate["status"], "activated")
            paths = workflow_paths(project)
            self.assertTrue((paths.planning_dir / "PLAN_DRAFT.md").is_file())
            self.assertIn("active: true", paths.plan_file.read_text(encoding="utf-8"))

    def test_shell_adapter_project_runs_validates_reconciles_and_final_verifies_through_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "init",
                    "--project",
                    str(project),
                    "--brief",
                    "Small Python shell smoke.",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            write_shell_smoke_plan(project)
            worker = write_shell_worker(project)
            configure_runner(
                project,
                "worker",
                adapter="shell",
                command=sys.executable,
                args=[worker.as_posix()],
                prompt_delivery={"mode": "stdin"},
                timeout_seconds=10,
                enabled=True,
            )
            configure_fake_objective_verifier(project)
            for runner_id in ("validator", "final_reviewer", "summary"):
                set_runner_enabled(project, runner_id, False)

            run = run_json("run", "--project", str(project), "--max-ticks", "1", "--json")

            execution = run["selected_action"]["execution_result"]
            self.assertEqual(run["selected_action"]["action"], "run_worker")
            self.assertEqual(execution["classification"], "worker_agent_status")
            self.assertEqual(execution["status"], "completed")
            run_dir = project / execution["role_output_dir"]
            self.assertTrue((run_dir / "artifacts" / "result.txt").is_file())

            validation = run_json(
                "validate",
                "--project",
                str(project),
                "--task",
                "T001",
                "--run-dir",
                str(run_dir),
                "--json",
            )
            reconcile = run_json(
                "reconcile",
                "--project",
                str(project),
                "--task",
                "T001",
                "--run-dir",
                str(run_dir),
                "--json",
            )
            final = run_json("final-verify", "--project", str(project), "--json")

            self.assertEqual(validation["status"], "pass")
            self.assertEqual(reconcile["status"], "reconciled")
            self.assertEqual(final["status"], "pass")
            paths = workflow_paths(project)
            self.assertTrue((paths.results_dir / "T001" / "latest.json").is_file())
            self.assertTrue((paths.runtime_dir / "plan_loop_complete.json").is_file())
            self.assertIn("- [x] T001: Run Python shell worker", paths.plan_file.read_text(encoding="utf-8"))

    def test_codex_cli_worker_project_runs_validates_reconciles_and_final_verifies_through_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            init = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "init",
                    "--project",
                    str(project),
                    "--brief",
                    "Small Codex CLI worker smoke.",
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
            write_codex_smoke_plan(project)
            fixture_bin = install_cli_adapter_fixture_bin(root)
            env = dict(os.environ)
            env["PATH"] = fixture_bin.as_posix() + os.pathsep + env.get("PATH", "")

            configured = run_json(
                "configure-agent",
                "--project",
                str(project),
                "--role",
                "worker",
                "--adapter",
                "codex_cli",
                "--command",
                "codex",
                "--json",
                env=env,
            )
            self.assertEqual(configured["runners"]["worker"]["adapter"], "codex_cli")
            configure_fake_objective_verifier(project)
            for runner_id in ("final_reviewer", "summary"):
                set_runner_enabled(project, runner_id, False)

            run = run_json("run", "--project", str(project), "--max-ticks", "1", "--json", env=env)
            execution = run["selected_action"]["execution_result"]
            self.assertEqual(run["selected_action"]["action"], "run_worker")
            self.assertEqual(execution["adapter"], "codex_cli")
            self.assertEqual(execution["runner_id"], "worker")
            self.assertEqual(execution["classification"], "worker_agent_status")
            self.assertEqual(execution["status"], "completed")

            run_dir = project / execution["role_output_dir"]
            paths = workflow_paths(project)
            scheduler_run_dir = paths.runtime_dir / "runs" / execution["run_id"]
            adapter_input = json.loads((scheduler_run_dir / "adapter_input.json").read_text(encoding="utf-8"))
            adapter_result = json.loads((scheduler_run_dir / "adapter_result.json").read_text(encoding="utf-8"))
            self.assertEqual(adapter_input["adapter"], "codex_cli")
            self.assertEqual(adapter_result["adapter"], "codex_cli")
            self.assertTrue((run_dir / "artifacts" / "result.txt").is_file())
            self.assertTrue((run_dir / "codex_fixture_record.json").is_file())

            validation = run_json(
                "validate",
                "--project",
                str(project),
                "--task",
                "P0.T001",
                "--run-dir",
                str(run_dir),
                "--json",
                env=env,
            )
            reconcile = run_json(
                "reconcile",
                "--project",
                str(project),
                "--task",
                "P0.T001",
                "--run-dir",
                str(run_dir),
                "--json",
            )
            final = run_json("run", "--project", str(project), "--max-ticks", "2", "--json", env=env)

            self.assertEqual(validation["status"], "pass")
            self.assertEqual(validation["accepted_task_ids"], ["P0.T001"])
            self.assertEqual(reconcile["status"], "reconciled")
            self.assertEqual(
                [item["action"] for item in final["action_history"]],
                ["run_final_verification"],
            )
            self.assertEqual(final["selected_action"]["action"], "run_final_verification")
            self.assertEqual(final["selected_action"]["execution_result"]["status"], "pass")
            paths = workflow_paths(project)
            self.assertTrue((paths.results_dir / "P0.T001" / "latest.json").is_file())
            self.assertTrue((paths.runtime_dir / "plan_loop_complete.json").is_file())
            self.assertIn(
                "- [x] P0.T001: Run Codex CLI worker fixture",
                paths.plan_file.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
