from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from runtime.init_workflow import init_project


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def set_approval_enabled(project: Path, enabled: bool) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["approval"]["enabled"] = enabled
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_approval_request(project: Path, approval_id: str, task_id: str = "T001") -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    append_jsonl(
        project / ".loopplane" / "runtime" / "human_approval_requests.jsonl",
        {
            "schema_version": "1.5",
            "approval_id": approval_id,
            "created_at": timestamp(),
            "workflow_id": workflow["workflow_id"],
            "task_id": task_id,
            "run_id": "run_cli_fixture",
            "type": "task_execution",
            "message": f"Approve {task_id}.",
            "scope": f"{task_id} only",
            "expires_at": "2099-01-01T00:00:00Z",
            "status": "pending",
        },
    )


class ApprovalCliTest(unittest.TestCase):
    def test_approvals_lists_pending_requests_and_disabled_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval CLI list.")
            append_approval_request(project, "approval_cli_list")

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "approvals", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "approval_disabled")
            self.assertFalse(payload["approval_policy"]["enabled"])
            self.assertEqual(payload["pending_count"], 1)
            self.assertEqual(payload["approvals"][0]["approval_id"], "approval_cli_list")

    def test_approve_refuses_when_interactive_approval_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval disabled refusal.")
            append_approval_request(project, "approval_disabled")

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "approve", "approval_disabled", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 1, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "approval_disabled")
            self.assertEqual(read_jsonl(project / ".loopplane" / "runtime" / "human_approval_responses.jsonl"), [])

    def test_approve_override_records_response_when_interactive_approval_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval disabled override.")
            append_approval_request(project, "approval_override")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "approve",
                    "approval_override",
                    "--project",
                    str(project),
                    "--override-disabled-policy",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "approved")
            self.assertTrue(payload["warnings"])
            self.assertEqual(payload["response"]["decision"], "approved")
            self.assertIn("override_disabled_policy", payload["response"]["source"])
            responses = read_jsonl(project / ".loopplane" / "runtime" / "human_approval_responses.jsonl")
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["approval_id"], "approval_override")

    def test_enabled_approve_and_reject_write_spec_compatible_responses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval enabled responses.")
            set_approval_enabled(project, True)
            append_approval_request(project, "approval_accept", task_id="T001")
            append_approval_request(project, "approval_expire", task_id="T002")

            approve = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "approve",
                    "approval_accept",
                    "--project",
                    str(project),
                    "--approved-by",
                    "tester",
                    "--notes",
                    "Approved for fixture.",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            expire = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "reject",
                    "approval_expire",
                    "--decision",
                    "expired",
                    "--project",
                    str(project),
                    "--approved-by",
                    "tester",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(approve.returncode, 0, approve.stderr + approve.stdout)
            self.assertEqual(expire.returncode, 0, expire.stderr + expire.stdout)
            approved_payload = json.loads(approve.stdout)
            expired_payload = json.loads(expire.stdout)
            self.assertEqual(approved_payload["response"]["decision"], "approved")
            self.assertEqual(expired_payload["response"]["decision"], "expired")
            responses = read_jsonl(project / ".loopplane" / "runtime" / "human_approval_responses.jsonl")
            self.assertEqual([response["decision"] for response in responses], ["approved", "expired"])
            for response in responses:
                self.assertEqual(response["schema_version"], "1.5")
                self.assertIn("approval_id", response)
                self.assertIn("responded_at", response)
                self.assertIn("approved_by", response)
                self.assertIn("scope", response)


if __name__ == "__main__":
    unittest.main()
