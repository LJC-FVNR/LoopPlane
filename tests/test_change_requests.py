from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import patch

from runtime.change_requests import (
    apply_change_request,
    change_request_exit_code,
    load_change_request_status,
    review_change_request,
    submit_change_request,
)
from runtime.dashboard import render_static_dashboard
from runtime.init_workflow import init_project
from runtime.inspector import answer_inspection
from runtime.read_models import rebuild_read_models
from tests.test_inspector import configure_fake_inspector
from tests.test_validation import write_plan


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def set_approval_enabled(project: Path, enabled: bool) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["approval"]["enabled"] = enabled
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def configure_fake_change_request_planner(project: Path) -> None:
    script = project / ".loopplane" / "config" / "fake_change_request_planner.py"
    script.write_text(
        r'''
import json
import os
import pathlib
import re
import sys


def title_from_request(text: str) -> str:
    value = " ".join(text.strip().split())
    value = re.sub(r"^(please\s+)?(add|change|update|modify|create)\s+", "", value, flags=re.IGNORECASE)
    value = value.rstrip(".") or "Apply approved change request"
    return value[:1].upper() + value[1:160]


prompt = sys.stdin.read()
match = re.search(r"## Change Request\s+```text\s*(.*?)\s*```", prompt, flags=re.DOTALL)
user_request = match.group(1).strip() if match else "Apply approved change request"
change_request_id = os.environ.get("LOOPPLANE_CHANGE_REQUEST_ID", "cr_fixture")
patch_path = pathlib.Path(os.environ["LOOPPLANE_PLAN_PATCH_PATH"])
response_path = pathlib.Path(os.environ["LOOPPLANE_CHANGE_REQUEST_RESPONSE_PATH"])
task_id = "CR.T001"
task_title = title_from_request(user_request)
patch_path.parent.mkdir(parents=True, exist_ok=True)
patch_path.write_text(
    f"""# LoopPlane PLAN_PATCH

- schema_version: 1.6
- change_request_id: {change_request_id}
- type: append_tasks
- target_phase: Phase Change Requests

This patch was proposed by the change request planner agent fixture.

LOOPPLANE_PLAN_APPEND_BEGIN
## Phase Change Requests: Approved Change Requests

- [ ] {task_id}: {task_title}
  - acceptance: The approved change request {change_request_id} is satisfied: {user_request}
  - evidence: .loopplane/results/{task_id}/
  - latest: .loopplane/results/{task_id}/latest.json
  - depends_on: []
  - risk: medium
  - validation: Focused validation demonstrates the requested plan change has been implemented.
  - max_attempts: 3
  - approval: required: change_request_scope
  - deliverables: Implementation and evidence for change request {change_request_id}
LOOPPLANE_PLAN_APPEND_END
""",
    encoding="utf-8",
)
response_path.write_text(
    json.dumps(
        {
            "status": "proposal_created",
            "impact": {
                "scope_change": True,
                "requires_approval": True,
                "adds_tasks": [task_id],
                "modifies_tasks": [],
                "supersedes_tasks": [],
            },
            "plan_patch": {"type": "append_tasks", "patch_file": str(patch_path)},
            "can_continue_before_resolution": False,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake change request planner wrote PLAN_PATCH.md")
''',
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["change_request_planner"].update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "enabled": True,
        }
    )
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_fake_structural_change_request_planner(project: Path) -> None:
    script = project / ".loopplane" / "config" / "fake_structural_change_request_planner.py"
    script.write_text(
        r'''
import json
import os
import pathlib

change_request_id = os.environ.get("LOOPPLANE_CHANGE_REQUEST_ID", "cr_fixture")
patch_path = pathlib.Path(os.environ["LOOPPLANE_PLAN_PATCH_PATH"])
response_path = pathlib.Path(os.environ["LOOPPLANE_CHANGE_REQUEST_RESPONSE_PATH"])
patch_path.parent.mkdir(parents=True, exist_ok=True)
patch_path.write_text(
    f"""# LoopPlane PLAN_PATCH

- schema_version: 1.5
- change_request_id: {change_request_id}
- type: append_tasks
- plan_patch_operation: insert_task_into_phase
- target_phase_id: P0

LOOPPLANE_PLAN_APPEND_BEGIN
## Phase P0: Validation Fixture

- [ ] CR.T002: Run the approved replacement path
  - acceptance: The replacement path is complete.
  - evidence: .loopplane/results/CR.T002/
  - latest: .loopplane/results/CR.T002/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 2
  - approval: not_required
  - deliverables: report.md
LOOPPLANE_PLAN_APPEND_END
""",
    encoding="utf-8",
)
response_path.write_text(
    json.dumps(
        {
            "status": "proposal_created",
            "impact": {
                "scope_change": True,
                "requires_approval": True,
                "adds_tasks": ["CR.T002"],
                "modifies_tasks": [],
                "supersedes_tasks": ["T001"],
            },
            "plan_patch": {
                "type": "append_tasks",
                "operation": "insert_task_into_phase",
                "target_phase_id": "P0",
                "patch_file": str(patch_path),
            },
            "can_continue_before_resolution": False,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake structural change request planner wrote PLAN_PATCH.md")
''',
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["change_request_planner"].update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "enabled": True,
        }
    )
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_fake_replacement_change_request_planner(project: Path) -> None:
    script = project / ".loopplane" / "config" / "fake_replacement_change_request_planner.py"
    script.write_text(
        r'''
import hashlib
import json
import os
import pathlib

plan_path = pathlib.Path(os.environ["LOOPPLANE_PLAN_FILE"])
patch_path = pathlib.Path(os.environ["LOOPPLANE_PLAN_PATCH_PATH"])
response_path = pathlib.Path(os.environ["LOOPPLANE_CHANGE_REQUEST_RESPONSE_PATH"])
change_request_id = os.environ["LOOPPLANE_CHANGE_REQUEST_ID"]
plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
patch_path.parent.mkdir(parents=True, exist_ok=True)
patch_path.write_text(
    f"""# LoopPlane PLAN_PATCH

- change_request_id: {change_request_id}
- type: replace_tasks
- target_plan_sha256: sha256:{plan_sha}

LOOPPLANE_PLAN_REPLACE_BEGIN
- [ ] T001: Produce result artifact
  - acceptance: Result artifact exists and the strengthened contract is recorded.
  - acceptance: Worker report records the completed command.
  - notes: Replacement fixture preserves the task identity and required fields.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt.
LOOPPLANE_PLAN_REPLACE_END
""",
    encoding="utf-8",
)
response_path.write_text(
    json.dumps(
        {
            "status": "patch_proposed",
            "impact": {
                "scope_change": True,
                "requires_approval": True,
                "adds_tasks": [],
                "modifies_tasks": ["T001"],
                "supersedes_tasks": [],
            },
            "plan_patch": {"type": "replace_tasks", "patch_file": str(patch_path)},
            "can_continue_before_resolution": False,
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake change request planner wrote replacement PLAN_PATCH.md")
''',
        encoding="utf-8",
    )
    config_path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["change_request_planner"].update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "enabled": True,
        }
    )
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_json(args: list[str]) -> tuple[int, dict[str, Any], str]:
    completed = subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
    return completed.returncode, payload, completed.stderr + completed.stdout


class ChangeRequestProtocolTest(unittest.TestCase):
    def test_change_request_to_approved_patch_flow_updates_plan_through_reconciler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Change request protocol smoke.")
            configure_fake_change_request_planner(project)
            set_approval_enabled(project, True)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")

            code, submitted, output = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Add a benchmark comparison task.",
                    "--json",
                ]
            )
            self.assertEqual(code, 0, output)
            change_request_id = submitted["change_request_id"]
            self.assertEqual(submitted["status"], "pending_review")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)

            code, reviewed, output = run_json(
                [
                    "change-request",
                    "review",
                    change_request_id,
                    "--project",
                    str(project),
                    "--audit",
                    "--json",
                ]
            )
            self.assertEqual(code, 0, output)
            self.assertEqual(reviewed["status"], "needs_user_approval")
            self.assertEqual(reviewed["role"], "change_request_planner")
            self.assertEqual(reviewed["runner_id"], "change_request_planner")
            self.assertEqual(reviewed["response"]["planner_source"], "change_request_planner_agent")
            self.assertTrue(reviewed["response"]["agent_run"]["ok"])
            role_output_dir = project / reviewed["role_output_dir"]
            self.assertEqual(role_output_dir.parent, project / ".loopplane" / "requests" / "change_runs")
            self.assertTrue(role_output_dir.is_dir())
            prompt_text = (role_output_dir / "change_request_planner_prompt.md").read_text(encoding="utf-8")
            self.assertIn("change_request_context_manifest.json", prompt_text)
            self.assertNotIn("# Project Plan", prompt_text)
            context_manifest = json.loads((role_output_dir / "change_request_context_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(context_manifest["references"]["active_plan"]["path"], "PLAN.md")
            self.assertIn("# Project Plan", context_manifest["references"]["active_plan"]["excerpt"])
            approval_id = reviewed["response"]["approval_request_id"]
            self.assertEqual(reviewed["response"]["role"], "change_request_planner")
            self.assertEqual(reviewed["response"]["role_output_dir"], reviewed["role_output_dir"])
            patch_file = project / reviewed["response"]["plan_patch"]["patch_file"]
            self.assertTrue(patch_file.is_file())
            self.assertEqual(patch_file.parent, role_output_dir)
            self.assertTrue((role_output_dir / "plan_patch_audit.json").is_file())
            node_summary = json.loads((role_output_dir / "node_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(node_summary["role"], "change_request_planner")
            self.assertEqual(node_summary["runner_id"], "change_request_planner")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)
            approvals = read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl")
            self.assertEqual(approvals[-1]["approval_id"], approval_id)
            self.assertEqual(approvals[-1]["type"], "change_request_plan_patch")
            self.assertEqual(approvals[-1]["change_request_id"], change_request_id)
            self.assertEqual(approvals[-1]["plan_patch_sha256"], reviewed["response"]["plan_patch"]["sha256"])
            self.assertEqual(
                reviewed["audit_report"]["plan_patch_sha256"],
                reviewed["response"]["plan_patch"]["sha256"],
            )

            code, approved, output = run_json(
                [
                    "approve",
                    approval_id,
                    "--project",
                    str(project),
                    "--approved-by",
                    "tester",
                    "--json",
                ]
            )
            self.assertEqual(code, 0, output)
            self.assertEqual(approved["status"], "approved")

            status_after_approval = load_change_request_status(project, change_request_id=change_request_id, include_all=True)
            self.assertEqual(status_after_approval["change_requests"][0]["status"], "approved")

            code, applied, output = run_json(
                [
                    "change-request",
                    "apply",
                    change_request_id,
                    "--project",
                    str(project),
                    "--json",
                ]
            )
            self.assertEqual(code, 0, output)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["apply_result"]["added_tasks"], ["CR.T001"])
            self.assertIn("- [ ] CR.T001: A benchmark comparison task", (project / "PLAN.md").read_text(encoding="utf-8"))
            self.assertNotEqual(file_hash(project / "PLAN.md"), original_plan_hash)
            self.assertEqual(applied["apply_result"]["event_id"], applied["apply_response"]["applied_plan_update_event_id"])
            self.assertIsNotNone(applied["apply_response"]["before_checkpoint_id"])
            self.assertIsNotNone(applied["apply_response"]["after_checkpoint_id"])

            responses = read_jsonl(project / ".loopplane" / "requests" / "change_request_responses.jsonl")
            self.assertEqual(responses[-1]["change_request_status"], "applied")
            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertIn("change_request_plan_patch_applied", [event["event_type"] for event in events])
            self.assertIn("change_request_applied", [event["event_type"] for event in events])
            checkpoints = read_jsonl(project / ".loopplane" / "runtime" / "git_checkpoints.jsonl")
            reasons = [checkpoint["reason"] for checkpoint in checkpoints]
            self.assertIn("before_change_request_apply", reasons)
            self.assertIn("after_change_request_apply", reasons)

    def test_apply_preserves_structural_routing_and_supersedes_pending_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Structural change request semantics.")
            configure_fake_structural_change_request_planner(project)
            write_plan(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            failure_registry = {
                "schema_version": "1.5",
                "workflow_id": workflow["workflow_id"],
                "failures": [
                    {
                        "failure_id": "fail_fixture",
                        "task_id": "T001",
                        "status": "unrecovered",
                        "recoverable": True,
                    }
                ],
            }
            (project / ".loopplane" / "runtime" / "failure_registry.json").write_text(
                json.dumps(failure_registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            submitted = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Replace the failed task with a corrected path.",
                    "--json",
                ]
            )[1]
            change_request_id = submitted["change_request_id"]
            code, reviewed, output = run_json(
                ["change-request", "review", change_request_id, "--project", str(project), "--json"]
            )
            self.assertEqual(code, 0, output)
            self.assertEqual(reviewed["status"], "approved")

            code, applied, output = run_json(
                ["change-request", "apply", change_request_id, "--project", str(project), "--json"]
            )
            self.assertEqual(code, 0, output)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["apply_result"]["plan_patch_operation"], "insert_task_into_phase")
            self.assertEqual(applied["apply_result"]["target_phase_id"], "P0")
            self.assertEqual(applied["apply_result"]["superseded_tasks"], ["T001"])
            self.assertEqual(applied["apply_result"]["recovered_superseded_failure_ids"], ["fail_fixture"])

            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertEqual(plan_text.count("## Phase P0:"), 1)
            self.assertIn("- [-] T001: Produce result artifact", plan_text)
            self.assertIn(f"skip_authorization: change_request:{change_request_id}", plan_text)
            self.assertIn("- [ ] CR.T002: Run the approved replacement path", plan_text)
            registry = json.loads((project / ".loopplane" / "runtime" / "failure_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["failures"][0]["status"], "recovered")
            self.assertEqual(registry["failures"][0]["resolution"], "superseded_by_approved_change_request")

    def test_change_request_replaces_existing_task_through_reconciler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Change request replacement protocol.")
            configure_fake_replacement_change_request_planner(project)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")

            submitted = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Strengthen the existing T001 acceptance contract.",
                    "--json",
                ]
            )[1]
            change_request_id = submitted["change_request_id"]
            code, reviewed, output = run_json(
                [
                    "change-request",
                    "review",
                    change_request_id,
                    "--project",
                    str(project),
                    "--audit",
                    "--json",
                ]
            )

            self.assertEqual(code, 0, output)
            self.assertEqual(reviewed["status"], "approved")
            self.assertEqual(reviewed["response"]["plan_patch"]["type"], "replace_tasks")
            self.assertEqual(reviewed["response"]["impact"]["adds_tasks"], [])
            self.assertEqual(reviewed["response"]["impact"]["modifies_tasks"], ["T001"])
            self.assertTrue(reviewed["audit_report"]["passed"])
            self.assertEqual(reviewed["audit_report"]["modified_tasks"], ["T001"])
            prompt = (project / reviewed["role_output_dir"] / "change_request_planner_prompt.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("LOOPPLANE_PLAN_REPLACE_BEGIN", prompt)
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)

            code, applied, output = run_json(
                ["change-request", "apply", change_request_id, "--project", str(project), "--json"]
            )

            self.assertEqual(code, 0, output)
            self.assertEqual(applied["status"], "applied")
            self.assertEqual(applied["apply_result"]["added_tasks"], [])
            self.assertEqual(applied["apply_result"]["modified_tasks"], ["T001"])
            self.assertEqual(applied["apply_result"]["plan_patch_operation"], "replace_tasks")
            plan_text = (project / "PLAN.md").read_text(encoding="utf-8")
            self.assertEqual(plan_text.count("- [ ] T001: Produce result artifact"), 1)
            self.assertIn("strengthened contract is recorded", plan_text)
            self.assertIn("Replacement fixture preserves the task identity", plan_text)
            self.assertIn("- latest: .loopplane/results/T001/latest.json", plan_text)

    def test_apply_requires_approval_and_preserves_plan_before_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Change request approval gate.")
            configure_fake_change_request_planner(project)
            set_approval_enabled(project, True)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")

            submit = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Add release note task.",
                    "--json",
                ]
            )[1]
            change_request_id = submit["change_request_id"]
            review = run_json(
                ["change-request", "review", change_request_id, "--project", str(project), "--json"]
            )[1]
            self.assertTrue(review["response"]["approval_request_id"])

            code, applied, output = run_json(
                ["change-request", "apply", change_request_id, "--project", str(project), "--json"]
            )

            self.assertEqual(code, 1, output)
            self.assertEqual(applied["status"], "approval_required")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)
            self.assertNotIn("CR.T001", (project / "PLAN.md").read_text(encoding="utf-8"))

    def test_failed_plan_patch_audit_returns_failure_and_cannot_be_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Audit failure must be an application gate.")
            configure_fake_change_request_planner(project)
            set_approval_enabled(project, True)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")
            submitted = submit_change_request(project, "Add an invalid audited task.")

            with patch(
                "runtime.change_requests._write_plan_patch_audit",
                return_value={
                    "schema_version": "1.5",
                    "status": "failed",
                    "passed": False,
                    "blocking_findings": ["fixture audit rejection"],
                },
            ):
                reviewed = review_change_request(
                    project,
                    submitted["change_request_id"],
                    audit=True,
                )

            self.assertFalse(reviewed["ok"])
            self.assertEqual(change_request_exit_code(reviewed), 1)
            self.assertEqual(reviewed["status"], "failed")
            self.assertEqual(reviewed["errors"], ["fixture audit rejection"])
            self.assertIsNone(reviewed["approval_request"])
            self.assertEqual(read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl"), [])

            applied = apply_change_request(project, submitted["change_request_id"])

            self.assertFalse(applied["ok"])
            self.assertEqual(applied["status"], "audit_failed")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)

    def test_approved_change_request_rejects_plan_patch_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approved change request content must remain immutable.")
            configure_fake_change_request_planner(project)
            set_approval_enabled(project, True)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")
            submitted = submit_change_request(project, "Add an immutable approved task.")
            reviewed = review_change_request(project, submitted["change_request_id"], audit=True)
            approval_id = reviewed["response"]["approval_request_id"]
            code, _approved, output = run_json(
                ["approve", approval_id, "--project", str(project), "--approved-by", "tester", "--json"]
            )
            self.assertEqual(code, 0, output)
            patch_path = project / reviewed["response"]["plan_patch"]["patch_file"]
            patch_path.write_text(
                patch_path.read_text(encoding="utf-8") + "\n<!-- changed after approval -->\n",
                encoding="utf-8",
            )

            applied = apply_change_request(project, submitted["change_request_id"])

            self.assertFalse(applied["ok"])
            self.assertEqual(applied["status"], "plan_patch_content_changed")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)

    def test_approval_disabled_review_auto_approves_without_approval_request_or_plan_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Change request approval disabled.")
            configure_fake_change_request_planner(project)
            write_plan(project)
            original_plan_hash = file_hash(project / "PLAN.md")

            submit = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Add packaging checklist.",
                    "--json",
                ]
            )[1]
            change_request_id = submit["change_request_id"]
            code, reviewed, output = run_json(
                ["change-request", "review", change_request_id, "--project", str(project), "--json"]
            )

            self.assertEqual(code, 0, output)
            self.assertEqual(reviewed["status"], "approved")
            self.assertIsNone(reviewed["response"]["approval_request_id"])
            self.assertEqual(read_jsonl(project / ".loopplane" / "runtime" / "human_approval_requests.jsonl"), [])
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)
            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "change_request_reviewed")

    def test_change_request_review_rejects_wrong_role_runner_before_plan_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Change request role boundary.")
            write_plan(project)
            submit = run_json(
                [
                    "change-request",
                    "submit",
                    "--project",
                    str(project),
                    "Add a docs task.",
                    "--json",
                ]
            )[1]
            config_path = project / ".loopplane" / "config" / "agent_runners.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["runners"]["change_request_planner"]["role"] = "worker"
            config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            code, reviewed, output = run_json(
                [
                    "change-request",
                    "review",
                    submit["change_request_id"],
                    "--project",
                    str(project),
                    "--json",
                ]
            )

            self.assertNotEqual(code, 0, output)
            self.assertEqual(reviewed["status"], "waiting_config")
            self.assertIn("expected 'change_request_planner'", "\n".join(reviewed["errors"]))
            self.assertFalse((project / ".loopplane" / "requests" / "change_runs").exists())

    def test_inspector_and_dashboard_change_paths_do_not_mutate_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Inspector dashboard boundary.")
            configure_fake_inspector(project)
            write_plan(project)
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            original_plan_hash = file_hash(project / "PLAN.md")

            inspection = answer_inspection(project, "Add a final release checklist task.")
            self.assertTrue(inspection["ok"], json.dumps(inspection, indent=2, sort_keys=True))
            self.assertEqual(inspection["status"], "answered")
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)

            render = render_static_dashboard(project, rebuild_read_models_first=True)
            self.assertTrue(render["ok"], json.dumps(render, indent=2, sort_keys=True))
            self.assertEqual(file_hash(project / "PLAN.md"), original_plan_hash)
            self.assertIn("change_requests", render["covered_sections"])
            html = (project / render["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Change Requests", html)
            self.assertIn("loopplane change-request submit", html)


if __name__ == "__main__":
    unittest.main()
