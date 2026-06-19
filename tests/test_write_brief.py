from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import _workflow_config, init_project


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
EXIT_SUCCESS = 0
EXIT_INVALID_CONFIG = 2

CUSTOM_PATHS = {
    "brief_file": "docs/BRIEF.md",
    "plan_file": "plans/ACTIVE_PLAN.md",
    "shared_context_file": "workflow/context/SHARED.md",
    "results_dir": "artifacts/loopplane/results",
    "runtime_dir": "state/loopplane/runtime",
    "read_models_dir": "state/loopplane/read_models",
    "requests_dir": "inbox/loopplane/requests",
    "planning_dir": "plans/loopplane/planning",
    "version_control_config_file": "configuration/loopplane/version_control.json",
}


def run_loopplane(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        cwd=REPO_ROOT,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


class WriteBriefCliTest(unittest.TestCase):
    def test_write_brief_creates_missing_brief_and_rebuilds_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Initial brief.")
            brief = project / "PROJECT_BRIEF.md"
            brief.unlink()

            result = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--text",
                "Create the replacement brief.",
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], result.stdout)
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["brief_file"], "PROJECT_BRIEF.md")
            self.assertTrue(payload["changed"])
            self.assertTrue(payload["created"])
            self.assertIn("Create the replacement brief.", brief.read_text(encoding="utf-8"))
            self.assertEqual(payload["checkpoint"]["checkpoint"]["reason"], "brief_created")
            self.assertIsInstance(payload["checkpoint"]["checkpoint"]["included_paths"], dict)
            self.assertIn("count", payload["checkpoint"]["checkpoint"]["included_paths"])

            events = read_jsonl(project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl")
            self.assertEqual(events[-1]["event_type"], "project_brief_created")
            self.assertEqual(payload["event"]["event_id"], events[-1]["event_id"])
            workflow_status = json.loads(
                (project / ".loopplane" / "read_models" / "workflow_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(workflow_status["source_event_id"], payload["event"]["event_id"])
            self.assertEqual(payload["read_model_rebuild"]["status"], "rebuilt")

    def test_write_brief_refuses_overwrite_then_updates_with_force_and_invalidates_planning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Initial brief.")
            brief = project / "PROJECT_BRIEF.md"
            before = brief.read_text(encoding="utf-8")
            planning_state = project / ".loopplane" / "planning" / "planning_state.json"
            planning_state.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": json.loads(
                            (project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
                        )["workflow_id"],
                        "status": "ready_for_activation",
                        "ready_for_activation": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            refused = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--text",
                "Replacement brief.",
                "--json",
            )

            self.assertEqual(refused.returncode, EXIT_INVALID_CONFIG, refused.stderr + refused.stdout)
            refused_payload = json.loads(refused.stdout)
            self.assertFalse(refused_payload["ok"])
            self.assertEqual(refused_payload["status"], "overwrite_refused")
            self.assertEqual(brief.read_text(encoding="utf-8"), before)

            updated = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--text",
                "Replacement brief.",
                "--force",
                "--json",
            )

            self.assertEqual(updated.returncode, EXIT_SUCCESS, updated.stderr + updated.stdout)
            payload = json.loads(updated.stdout)
            self.assertTrue(payload["ok"], updated.stdout)
            self.assertEqual(payload["status"], "updated")
            self.assertTrue(payload["updated"])
            self.assertIn("Replacement brief.", brief.read_text(encoding="utf-8"))
            self.assertEqual(payload["checkpoint"]["checkpoint"]["reason"], "brief_updated")
            self.assertTrue(payload["planning_invalidated"])
            invalidated = json.loads(planning_state.read_text(encoding="utf-8"))
            self.assertEqual(invalidated["status"], "brief_changed")
            self.assertFalse(invalidated["ready_for_activation"])

    def test_write_brief_accepts_file_and_stdin_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Initial brief.")
            source_file = Path(tmp) / "brief.md"
            source_file.write_text("# Project Brief\n\n## User Request\n\nUse the file brief.\n", encoding="utf-8")

            from_file = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--file",
                str(source_file),
                "--force",
                "--json",
            )

            self.assertEqual(from_file.returncode, EXIT_SUCCESS, from_file.stderr + from_file.stdout)
            file_payload = json.loads(from_file.stdout)
            self.assertEqual(file_payload["status"], "updated")
            self.assertEqual((project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"), source_file.read_text(encoding="utf-8"))

            alias_file = Path(tmp) / "brief_alias.md"
            alias_file.write_text("# Project Brief\n\n## User Request\n\nUse the brief-file alias.\n", encoding="utf-8")
            from_alias = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--brief-file",
                str(alias_file),
                "--force",
                "--json",
            )

            self.assertEqual(from_alias.returncode, EXIT_SUCCESS, from_alias.stderr + from_alias.stdout)
            alias_payload = json.loads(from_alias.stdout)
            self.assertEqual(alias_payload["status"], "updated")
            self.assertEqual(alias_payload["source"], f"cli --file {alias_file.as_posix()}")
            self.assertEqual((project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"), alias_file.read_text(encoding="utf-8"))

            from_stdin = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--stdin",
                "--force",
                "--json",
                input_text="Use the stdin brief.",
            )

            self.assertEqual(from_stdin.returncode, EXIT_SUCCESS, from_stdin.stderr + from_stdin.stdout)
            stdin_payload = json.loads(from_stdin.stdout)
            self.assertEqual(stdin_payload["source"], "cli --stdin")
            self.assertIn("Use the stdin brief.", (project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"))

    def test_write_brief_rejects_missing_and_empty_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Initial brief.")

            missing = run_loopplane("write-brief", "--project", str(project), "--json")
            self.assertEqual(missing.returncode, EXIT_INVALID_CONFIG, missing.stderr + missing.stdout)
            self.assertEqual(json.loads(missing.stdout)["status"], "missing_input")

            empty = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--stdin",
                "--json",
                input_text=" \n\t",
            )
            self.assertEqual(empty.returncode, EXIT_INVALID_CONFIG, empty.stderr + empty.stdout)
            self.assertEqual(json.loads(empty.stdout)["status"], "empty_input")

    def test_write_brief_resolves_configured_brief_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workflow_path = project / ".loopplane" / "config" / "workflow.json"
            workflow_path.parent.mkdir(parents=True)
            workflow = _workflow_config("wf_20260611_1234abcd", "2026-06-11T00:00:00Z", CUSTOM_PATHS)
            workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            init_project(project, "Initial custom-path brief.")
            custom_brief = project / CUSTOM_PATHS["brief_file"]
            custom_brief.unlink()

            result = run_loopplane(
                "write-brief",
                "--project",
                str(project),
                "--text",
                "Write through configured paths.",
                "--json",
            )

            self.assertEqual(result.returncode, EXIT_SUCCESS, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["brief_file"], CUSTOM_PATHS["brief_file"])
            self.assertTrue(custom_brief.is_file())
            self.assertFalse((project / "PROJECT_BRIEF.md").exists())
            self.assertIn("Write through configured paths.", custom_brief.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
