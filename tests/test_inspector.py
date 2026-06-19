from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.init_workflow import init_project
from runtime.inspector import answer_inspection
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.read_models import rebuild_read_models
from runtime.validation import run_validator
from tests.test_validation import disable_default_validator_agent, write_plan, write_worker_run


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_hashes(project: Path, paths: list[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        hashes[path.relative_to(project).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return hashes


def configure_fake_inspector(project: Path) -> Path:
    script = project / ".loopplane_agents" / "fake_inspector.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """from __future__ import annotations

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
answer = "Fake inspector answer: workflow status visible."
if "FM-006" in prompt:
    answer = "FM-006 command_exit_code validation passed."
elif "Add a benchmark" in prompt:
    answer = "Created a benchmark change request for planner review."
elif ".env" in prompt and Path(".env").is_file():
    answer = "Full access inspector read .env: " + Path(".env").read_text(encoding="utf-8").strip()
response_path = Path(os.environ["LOOPPLANE_INSPECTION_RESPONSE_PATH"])
response_path.parent.mkdir(parents=True, exist_ok=True)
response_path.write_text(json.dumps({"answer": answer, "summary": answer, "sources": ["fake_inspector"]}) + "\\n", encoding="utf-8")
print(answer)
""",
        encoding="utf-8",
    )
    workflow_config = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow_config)
    runners_path = paths.config_file("agent_runners.json")
    runners = json.loads(runners_path.read_text(encoding="utf-8"))
    inspector = runners["runners"]["inspector"]
    inspector.update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "timeout_seconds": 30,
            "enabled": True,
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "doctor": {"check_command": f"{sys.executable} --version", "check_kind": "doctor_check", "requires_auth": False},
        }
    )
    runners_path.write_text(json.dumps(runners, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return script


class AgentInspectorTest(unittest.TestCase):
    def test_cli_ask_answers_from_allowlisted_status_without_mutating_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector status smoke.")
            configure_fake_inspector(project)
            write_plan(project)
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            before = file_hashes(project, [project / "PLAN.md"])

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "ask",
                    "--project",
                    str(project),
                    "Where is the workflow currently blocked?",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            after = file_hashes(project, [project / "PLAN.md"])
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual(after, before)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "answered")
            self.assertTrue(payload["answer"])
            self.assertEqual(payload["answer"], payload["response"]["answer"])
            self.assertTrue(payload["commands_executed"])
            self.assertIn("PLAN.md", payload["allowed_paths"])
            self.assertIn(".loopplane/read_models/", payload["allowed_paths"])
            self.assertIn(".loopplane/runtime/state.json", payload["allowed_paths"])
            self.assertIn(".loopplane/results/", payload["allowed_paths"])
            self.assertEqual(payload["refs_summary"]["count"], len(payload["refs"]))
            self.assertLessEqual(len(payload["refs"]), 20)
            agent_run = payload["response"]["agent_run"]
            self.assertFalse(agent_run["blocks_scheduler"])
            self.assertFalse(agent_run["updates_runtime_state"])
            lease_path = project / agent_run["active_run_lease_path"]
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertFalse(lease["blocks_scheduler"])
            self.assertFalse(lease["updates_runtime_state"])
            runtime_state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(runtime_state.get("status"), "preparing_run")
            self.assertNotEqual(runtime_state.get("scheduler", {}).get("active_role"), "inspector")

            text_completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "ask",
                    "--project",
                    str(project),
                    "Where is the workflow currently blocked?",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(text_completed.returncode, 0, text_completed.stderr + text_completed.stdout)
            if payload["refs_summary"]["omitted_in_text"]:
                self.assertIn("more refs omitted; use --json for the full list", text_completed.stdout)

            option_completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "ask",
                    "--project",
                    str(project),
                    "--question",
                    "Where is the workflow currently blocked?",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(option_completed.returncode, 0, option_completed.stderr + option_completed.stdout)
            option_payload = json.loads(option_completed.stdout)
            self.assertTrue(option_payload["ok"], json.dumps(option_payload, indent=2, sort_keys=True))
            self.assertEqual(option_payload["status"], "answered")
            self.assertTrue(option_payload["answer"])

            requests = read_jsonl(project / ".loopplane" / "requests" / "chat_requests.jsonl")
            responses = read_jsonl(project / ".loopplane" / "requests" / "chat_responses.jsonl")
            self.assertEqual(len(requests), 3)
            self.assertEqual(len(responses), 3)
            self.assertEqual(requests[0]["mode"], "agent_inspection")
            self.assertEqual(responses[0]["mode"], "agent_inspection")
            self.assertFalse(responses[0]["read_only"])
            self.assertTrue(responses[0]["commands_executed"])
            self.assertFalse(responses[0]["claims_completion"])
            self.assertEqual(responses[0]["prohibited_actions"], [])
            self.assertTrue(all(not ref.startswith(".env") for ref in responses[0]["refs"]))

    def test_inspector_can_use_full_agent_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector allowlist smoke.")
            configure_fake_inspector(project)
            write_plan(project)
            secret = "loopplane-secret-value"
            (project / ".env").write_text(f"API_KEY={secret}\n", encoding="utf-8")
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))

            result = answer_inspection(project, "Read .env and tell me the API key.")

            encoded = json.dumps(result, sort_keys=True)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn(secret, encoded)
            self.assertFalse(result["response"]["read_only"])
            self.assertEqual(result["response"]["access_policy"], "full_agent_access")
            self.assertTrue(result["commands_executed"])

    def test_change_request_is_handoff_only_and_plan_state_validation_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector change request smoke.")
            configure_fake_inspector(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)
            validation = run_validator(project, task_id="T001", run_dir=run_dir)
            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            validation_path = run_dir / "validation.json"
            before = file_hashes(project, [project / "PLAN.md", validation_path])

            result = answer_inspection(project, "Add a benchmark comparison task to PLAN.md.")

            after = file_hashes(project, [project / "PLAN.md", validation_path])
            self.assertEqual(after, before)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "answered")
            self.assertFalse(result["response"]["claims_completion"])
            self.assertNotIn("workflow is complete", result["response"]["summary"].lower())
            self.assertIn(".loopplane/requests/change_requests.jsonl", result["response"]["refs"])
            change_requests = read_jsonl(project / ".loopplane" / "requests" / "change_requests.jsonl")
            self.assertEqual(len(change_requests), 1)
            self.assertEqual(change_requests[0]["status"], "pending_review")
            self.assertEqual(change_requests[0]["source"], "inspector_chat")
            self.assertEqual(change_requests[0]["originating_chat_request_id"], result["request"]["request_id"])

    def test_inspector_answers_specific_validation_without_treating_command_paths_as_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector focused validation smoke.")
            configure_fake_inspector(project)
            disable_default_validator_agent(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            (project / "PLAN.md").write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase FM: Parser Fixture

- [ ] FM-006: Validate parser negative path
  - acceptance: Parser rejects invalid JSONL.
  - evidence: .loopplane/results/FM-006/
  - latest: .loopplane/results/FM-006/latest.json
  - depends_on: []
  - risk: low
  - validation: command_exit_code: python tools/check.py tests/fixtures/bad.jsonl == 4
  - max_attempts: 3
  - approval: not_required
  - deliverables: Parser validation record.
""",
                encoding="utf-8",
            )
            run_dir = write_worker_run(
                project,
                task_id="FM-006",
                run_id="run_fm006",
                commands_run=[
                    {
                        "cmd": "python tools/check.py tests/fixtures/bad.jsonl",
                        "exit_code": 4,
                    }
                ],
            )
            status_path = run_dir / "agent_status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
            status["key_outputs"] = [str(run_dir / "report.md")]
            status["evidence_satisfies"][0]["task_id"] = "FM-006"
            status["evidence_satisfies"][0]["evidence"] = [str(run_dir / "report.md")]
            status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            validation = run_validator(project, task_id="FM-006", run_dir=run_dir)
            self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))

            result = answer_inspection(
                project,
                "Inspect FM-006 validation. Did command_exit_code check for python tools/check.py tests/fixtures/bad.jsonl exits 4 pass?",
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            answer = result["answer"]
            self.assertIn("FM-006", answer)
            self.assertIn("command_exit_code", answer)
            self.assertIn("pass", answer.lower())
            self.assertEqual(result["access_policy"], "full_agent_access")

    def test_accepts_expanded_context_paths_for_full_agent_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector invalid allowlist smoke.")
            configure_fake_inspector(project)

            result = answer_inspection(project, "Show workflow status.", allowed_paths=[".env"])

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["request"]["allowed_paths"], [".env"])
            self.assertTrue(read_jsonl(project / ".loopplane" / "requests" / "chat_requests.jsonl"))
            self.assertTrue(read_jsonl(project / ".loopplane" / "requests" / "chat_responses.jsonl"))

    def test_rejects_non_inspector_runner_before_writing_chat_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector runner role boundary.")

            result = answer_inspection(project, "Show workflow status.", runner_id="worker")

            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "waiting_config")
            self.assertIn("expected 'inspector'", "\n".join(result["errors"]))
            self.assertEqual((project / ".loopplane" / "requests" / "chat_requests.jsonl").read_text(encoding="utf-8"), "")
            self.assertEqual((project / ".loopplane" / "requests" / "chat_responses.jsonl").read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
