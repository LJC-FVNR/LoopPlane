from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import runtime.human_summaries as human_summaries_module
from runtime.human_summaries import ensure_human_summaries
from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.reconciliation import run_reconciler
from runtime.validation import run_validator
from tests.test_validation import write_plan, write_worker_run


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def configure_fake_summary_agent(project: Path) -> None:
    paths = WorkflowPaths.from_config(project, load_workflow_config(project))
    workflow_path = paths.workflow_config_file
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow.setdefault("human_summaries", {})["auto_after_reconcile"] = True
    workflow["human_summaries"]["generation_mode"] = "automatic"
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config_path = paths.config_file("agent_runners.json")
    script = config_path.parent / "fake_summary_agent.py"
    script.write_text(
        r'''
import json
import os
import pathlib
import re
import sys


prompt = sys.stdin.read()
target_kind = os.environ.get("LOOPPLANE_SUMMARY_TARGET_KIND", "task")
target_id = os.environ.get("LOOPPLANE_SUMMARY_TARGET_ID", "unknown")
markdown_path = pathlib.Path(os.environ["LOOPPLANE_SUMMARY_MARKDOWN_PATH"])
json_path = pathlib.Path(os.environ["LOOPPLANE_SUMMARY_JSON_PATH"])
project = pathlib.Path(os.environ.get("LOOPPLANE_PROJECT_ROOT", ".")).resolve()
manifest_match = re.search(r'Read `([^`]*summary_context_manifest\.json)`', prompt)
if not manifest_match:
    raise SystemExit("summary context manifest was not referenced")


def project_path(value):
    path = pathlib.Path(str(value))
    return path if path.is_absolute() else project / path


manifest = json.loads(project_path(manifest_match.group(1)).read_text(encoding="utf-8"))


def load_reference_json(name, default):
    ref = manifest.get("references", {}).get(name, {})
    path = ref.get("path")
    if not path:
        return default
    try:
        return json.loads(project_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


visual_artifacts = load_reference_json("visual_artifacts", [])
evidence = load_reference_json("runtime_evidence", {})
artifact_path = ".loopplane/results/T001/runs/run_fixture/artifacts/failure_modes.svg"
for item in visual_artifacts:
    if isinstance(item, dict) and str(item.get("path", "")).endswith("failure_modes.svg"):
        artifact_path = str(item["path"])
        break

if target_kind == "phase":
    objective_rows = evidence.get("phase_objective_closure") if isinstance(evidence, dict) else []
    has_po1 = any(isinstance(row, dict) and str(row.get("Objective") or row.get("objective") or "") == "PO1" for row in objective_rows)
    objective = """
The declared handoff objective now has a visible confidence boundary: the artifact can support downstream decisions once closure is explicitly recorded.
""" if has_po1 else ""
    markdown = f"""# Validation Fixture: Phase Progress Reading

This phase turns a narrow validation fixture into a project-facing review layer. The value is not the existence of a completed work item; it is that qualitative case studies are now readable enough to inform downstream inspection and leadership confidence.
{objective}
"""
else:
    markdown = f"""# Result Artifact: Strategic Progress Reading

The completed work turns qualitative case studies and representative failure analysis into a clearer project asset. It gives leadership a sharper view of where retrieval quality is fragile, which mitigation themes are becoming concrete, and why the next review can focus on confidence rather than basic discovery.

Retrieval failures cluster around incomplete evidence metadata, and the summary separates case narrative from mitigation notes. That creates new information value: the project can now discuss failure modes as an interpretable pattern, not as scattered implementation notes.

[Open failure-mode view]({artifact_path})

![Failure-mode view]({artifact_path} "Failure-mode view")

[Open full-size failure-mode view]({artifact_path})
"""

markdown_path.parent.mkdir(parents=True, exist_ok=True)
markdown_path.write_text(markdown, encoding="utf-8")
json_path.parent.mkdir(parents=True, exist_ok=True)
json_path.write_text(
    json.dumps(
        {
            "status": "ready",
            "summary_title": f"{target_kind.title()} {target_id}",
            "summary_excerpt": "Agent-authored leadership summary focused on the strategic project increment.",
            "markdown_path": str(markdown_path),
            "generated_by": "summary_agent",
            "tables": {
                "failure_modes": [
                    {"Mode": "retrieval", "Count": "3", "Reading": "metadata gaps"}
                ]
            },
            "figures": [
                {
                    "label": "Failure modes",
                    "path": artifact_path,
                    "caption": "Representative failure-mode distribution."
                }
            ],
        },
        indent=2,
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
print("fake summary agent wrote markdown")
''',
        encoding="utf-8",
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["summary"].update(
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


def disable_summary_agent(project: Path) -> None:
    """Force the deterministic mechanical fallback by disabling the summary runner."""
    paths = WorkflowPaths.from_config(project, load_workflow_config(project))
    workflow_path = paths.workflow_config_file
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow.setdefault("human_summaries", {})["auto_after_reconcile"] = True
    workflow["human_summaries"]["generation_mode"] = "automatic"
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config_path = paths.config_file("agent_runners.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["summary"]["enabled"] = False
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_slow_summary_agent(project: Path) -> None:
    paths = WorkflowPaths.from_config(project, load_workflow_config(project))
    workflow_path = paths.workflow_config_file
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow.setdefault("human_summaries", {})["auto_after_reconcile"] = True
    workflow["human_summaries"]["generation_mode"] = "automatic"
    workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    config_path = paths.config_file("agent_runners.json")
    script = config_path.parent / "slow_summary_agent.py"
    script.write_text(
        r'''
import json
import os
import pathlib
import sys
import time


sys.stdin.read()
markdown_path = pathlib.Path(os.environ["LOOPPLANE_SUMMARY_MARKDOWN_PATH"])
json_path = pathlib.Path(os.environ["LOOPPLANE_SUMMARY_JSON_PATH"])
markdown_path.parent.mkdir(parents=True, exist_ok=True)
markdown_path.write_text("# Slow Summary\n\nThis summary is intentionally delayed.\n", encoding="utf-8")
json_path.parent.mkdir(parents=True, exist_ok=True)
json_path.write_text(json.dumps({"status": "ready", "summary_excerpt": "Delayed summary."}) + "\n", encoding="utf-8")
time.sleep(3)
''',
        encoding="utf-8",
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["runners"]["summary"].update(
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


class HumanSummaryTest(unittest.TestCase):
    def test_artifact_fingerprint_does_not_hash_each_artifact_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            artifacts_dir = run_dir / "artifacts"
            artifacts_dir.mkdir(parents=True)
            for index in range(300):
                (artifacts_dir / f"tensor_{index:04d}.bin").write_bytes(b"payload")

            with mock.patch.object(
                human_summaries_module,
                "_sha256_file",
                side_effect=AssertionError("artifact content hashing should not run"),
            ):
                fingerprint = human_summaries_module._artifact_tree_hash(run_dir)

            self.assertIsNotNone(fingerprint)
            self.assertTrue(str(fingerprint).startswith("sha256:"))

    def test_phase_summary_loads_legacy_highlight_report_task_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary legacy compatibility.")
            disable_summary_agent(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            (project / "PLAN.md").write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- workflow_title: Human Summary Legacy Fixture
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Legacy Summary Fixture

- [x] T001: Existing legacy highlight report
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - validation: file_exists: artifacts/result.txt

- [x] T002: New terminal task
  - evidence: .loopplane/results/T002/
  - latest: .loopplane/results/T002/latest.json
  - depends_on: []
  - validation: file_exists: artifacts/result.txt
""",
                encoding="utf-8",
            )
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            legacy_dir = paths.results_dir / "T001"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "human_summary.md").write_text("# Legacy Summary\n\nLegacy summary excerpt.\n", encoding="utf-8")
            (legacy_dir / "human_summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "kind": "highlight_report",
                        "status": "ready",
                        "workflow_id": workflow["workflow_id"],
                        "target_id": "T001",
                        "summary_title": "Legacy highlight report",
                        "summary_excerpt": "Legacy summary excerpt.",
                        "markdown_path": ".loopplane/results/T001/human_summary.md",
                        "tables": [
                            {
                                "title": "Legacy Table",
                                "columns": ["Metric", "Value"],
                                "rows": [["signal", "present"]],
                            }
                        ],
                        "figures": [],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = ensure_human_summaries(project, task_ids=["T002"], blocking=False, max_agent_summaries=0)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "summaries_updated")
            phase_json = paths.results_dir / "phases" / "P0" / "human_summary.json"
            phase_record = json.loads(phase_json.read_text(encoding="utf-8"))
            task_outcomes = phase_record["tables"]["task_outcomes"]
            self.assertEqual(task_outcomes[0]["Task"], "T001")
            self.assertIn("Legacy summary excerpt", task_outcomes[0]["Summary"])

    def test_reconciler_can_defer_human_summaries_for_on_demand_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary deferred smoke.")
            configure_fake_summary_agent(project)
            workflow_path = project / ".loopplane" / "config" / "workflow.json"
            workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
            workflow.setdefault("human_summaries", {})["auto_after_reconcile"] = False
            workflow_path.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)

            validation = run_validator(project, task_id="T001", run_dir=run_dir)
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass")
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))
            self.assertEqual(reconciliation["human_summaries"]["status"], "deferred")
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            self.assertFalse((paths.results_dir / "T001" / "human_summary.md").exists())

            current = ensure_human_summaries(project, task_ids=["T001"])

            self.assertEqual(current["status"], "summaries_updated")
            self.assertTrue((paths.results_dir / "T001" / "human_summary.md").is_file())

    def test_reconciler_schedules_slow_human_summary_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary non-blocking smoke.")
            configure_slow_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)

            self.assertEqual(run_validator(project, task_id="T001", run_dir=run_dir)["status"], "pass")
            started = time.monotonic()
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            elapsed = time.monotonic() - started

            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))
            self.assertLess(elapsed, 2.5)
            self.assertEqual(reconciliation["human_summaries"]["status"], "summaries_updated")
            self.assertEqual(reconciliation["human_summaries"]["scheduled_count"], 1)
            self.assertEqual(reconciliation["human_summaries"]["written_count"], 1)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            self.assertTrue((paths.results_dir / "phases" / "P0" / "human_summary.md").is_file())

    def test_slow_phase_summary_writes_fallback_and_tracks_background_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary slow phase smoke.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)

            self.assertEqual(run_validator(project, task_id="T001", run_dir=run_dir)["status"], "pass")
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))

            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            phase_markdown = paths.results_dir / "phases" / "P0" / "human_summary.md"
            phase_json = paths.results_dir / "phases" / "P0" / "human_summary.json"
            phase_markdown.unlink()
            phase_json.unlink()
            configure_slow_summary_agent(project)

            started = time.monotonic()
            refreshed = ensure_human_summaries(project, task_ids=["T001"], blocking=False, inline_wait_seconds=0.1)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 2.5)
            self.assertEqual(refreshed["status"], "summaries_updated")
            self.assertEqual(refreshed["written_count"], 1)
            self.assertEqual(refreshed["scheduled_count"], 1)
            self.assertEqual(refreshed["phase_results"][0]["status"], "ready")
            self.assertIn("background_summary_agent", refreshed["phase_results"][0])
            phase_record = json.loads(phase_json.read_text(encoding="utf-8"))
            self.assertEqual(phase_record["status"], "ready")
            self.assertNotEqual(phase_record.get("generated_by"), "summary_agent")
            time.sleep(3.2)

    def test_fallback_summary_is_honest_and_plain_without_traceability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary fallback tone.")
            disable_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                report_text=(
                    "# Worker Report\n\n"
                    "Implemented a result narrative in artifacts/result.txt, updated src/app.py, "
                    "and recorded validation details in agent_status.json so the workflow can confirm completion.\n\n"
                    "- The work clarifies which project promise is now visible for review.\n"
                ),
            )

            self.assertEqual(run_validator(project, task_id="T001", run_dir=run_dir)["status"], "pass")
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))

            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            task_content = (paths.results_dir / "T001" / "human_summary.md").read_text(encoding="utf-8")
            phase_content = (paths.results_dir / "phases" / "P0" / "human_summary.md").read_text(encoding="utf-8")

            # Honest, plain mechanical fallback: a status note, not fabricated
            # "strategic" leadership prose the fallback cannot substantiate.
            self.assertIn("status note", task_content)
            self.assertNotIn("Strategic Progress Update", task_content)
            self.assertNotIn("moved from planned intent", task_content)
            self.assertNotIn("\n## ", task_content)
            # Still leaks no runtime traceability / mechanics.
            for forbidden in (
                "Executive Brief",
                "Traceability",
                "Validation record",
                "Latest pointer",
                "Run folder",
                "agent_status.json",
                "artifacts/result.txt",
                "src/app.py",
                "changed file",
            ):
                self.assertNotIn(forbidden, task_content)
            self.assertIn("phase status note", phase_content)
            self.assertNotIn("reads as a coherent completed chapter", phase_content)
            self.assertNotIn("\n## ", phase_content)
            self.assertNotIn("| Task |", phase_content)

    def test_reconciler_writes_task_and_phase_human_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary smoke.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(
                project,
                create_artifact=True,
                report_text=(
                    "# Qualitative Case Study Progress Report\n\n"
                    "Implemented qualitative case studies for three representative failure modes. "
                    "The work compares successful and failed runs, identifies brittle prompt assumptions, "
                    "and records follow-up evaluation recommendations for the project lead.\n\n"
                    "| Signal | Management reading |\n"
                    "| --- | --- |\n"
                    "| Evidence metadata | Needs tighter completeness checks |\n\n"
                    "![Failure mode distribution](.loopplane/results/T001/runs/run_fixture/artifacts/failure_modes.png \"Failure mode distribution\")\n\n"
                    "Key findings:\n"
                    "- Retrieval failures cluster around incomplete evidence metadata.\n"
                    "- Visualization output now separates case narrative from mitigation notes.\n"
                ),
            )
            (run_dir / "artifacts" / "failure_modes.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40"><text x="4" y="24">modes</text></svg>\n',
                encoding="utf-8",
            )

            validation = run_validator(project, task_id="T001", run_dir=run_dir)
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)

            self.assertEqual(validation["status"], "pass")
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))
            self.assertEqual(reconciliation["human_summaries"]["status"], "summaries_updated")

            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            task_markdown = paths.results_dir / "T001" / "human_summary.md"
            task_json = paths.results_dir / "T001" / "human_summary.json"
            phase_markdown = paths.results_dir / "phases" / "P0" / "human_summary.md"
            phase_json = paths.results_dir / "phases" / "P0" / "human_summary.json"
            for path in (task_markdown, task_json, phase_markdown, phase_json):
                self.assertTrue(path.is_file(), path)

            task_record = json.loads(task_json.read_text(encoding="utf-8"))
            self.assertEqual(task_record["status"], "ready")
            self.assertEqual(task_record["task_id"], "T001")
            self.assertEqual(task_record["generated_by"], "summary_agent")
            self.assertEqual(task_record["tables"]["failure_modes"][0]["Mode"], "retrieval")
            self.assertEqual(task_record["figures"][0]["path"], ".loopplane/results/T001/runs/run_fixture/artifacts/failure_modes.svg")
            task_content = task_markdown.read_text(encoding="utf-8")
            self.assertIn("Strategic Progress Reading", task_record["content"])
            self.assertIn("clearer project asset", task_record["content"])
            self.assertIn("new information value", task_record["content"])
            self.assertNotIn("Executive Brief", task_record["content"])
            self.assertNotIn("Progress Narrative", task_record["content"])
            self.assertNotIn("Key Data", task_record["content"])
            self.assertNotIn("Evidence And Confidence", task_record["content"])
            self.assertIn("![Failure-mode view]", task_record["content"])
            self.assertIn("[Open failure-mode view]", task_record["content"])
            self.assertIn("[Open full-size failure-mode view]", task_record["content"])
            self.assertIn("qualitative case studies", task_content)
            self.assertIn("Retrieval failures cluster", task_content)
            self.assertIn("leadership", task_content)
            self.assertIn("failure_modes.svg", task_content)
            self.assertIn("[Open failure-mode view]", task_content)
            self.assertNotIn("For project control", task_content)
            self.assertNotIn("Qualitative Case Study Progress Report", task_content)
            self.assertNotIn("| Evidence metadata | Needs tighter completeness checks |", task_content)
            self.assertNotIn("![Failure mode distribution]", task_content)
            self.assertNotIn("Worker adapter completed and agent_status.json is available", task_content)
            self.assertNotIn("No caveats were recorded", task_content)
            self.assertNotIn("No changed-file table was recorded", task_content)

            phase_record = json.loads(phase_json.read_text(encoding="utf-8"))
            self.assertEqual(phase_record["status"], "ready")
            self.assertEqual(phase_record["phase_id"], "P0")
            self.assertEqual(phase_record["generated_by"], "summary_agent")
            self.assertEqual(phase_record["tables"]["failure_modes"][0]["Reading"], "metadata gaps")
            self.assertEqual(phase_record["figures"][0]["label"], "Failure modes")
            self.assertNotIn("| Task |", phase_record["content"])
            self.assertIn("project-facing review layer", phase_record["content"])
            self.assertIn("qualitative case studies", phase_record["content"])

    def test_cli_summarize_backfills_terminal_task_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary CLI smoke.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)
            self.assertEqual(run_validator(project, task_id="T001", run_dir=run_dir)["status"], "pass")
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))

            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            (paths.results_dir / "T001" / "human_summary.md").unlink()
            (paths.results_dir / "T001" / "human_summary.json").unlink()
            (paths.results_dir / "phases" / "P0" / "human_summary.md").unlink()
            (paths.results_dir / "phases" / "P0" / "human_summary.json").unlink()

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "summarize", "--project", str(project), "--task", "T001", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["written_count"], 2)
            self.assertTrue((paths.results_dir / "T001" / "human_summary.md").is_file())
            self.assertTrue((paths.results_dir / "phases" / "P0" / "human_summary.md").is_file())

            current = ensure_human_summaries(project, task_ids=["T001"])
            self.assertEqual(current["status"], "current")

    def test_phase_summary_includes_objective_closure_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Human summary objective table.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8")
                + """

### Phase Objective Checklist

- [ ] `PO1` Result artifact supports downstream handoff.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Decide whether downstream users can rely on the result artifact.
  - verifier: objective_verifier
  - unmet_action: self_expand
""",
                encoding="utf-8",
            )
            run_dir = write_worker_run(project, create_artifact=True)
            self.assertEqual(run_validator(project, task_id="T001", run_dir=run_dir)["status"], "pass")
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))

            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            phase_json = paths.results_dir / "phases" / "P0" / "human_summary.json"
            phase_record = json.loads(phase_json.read_text(encoding="utf-8"))

            self.assertIn("handoff objective", phase_record["content"])
            self.assertEqual(phase_record["tables"]["phase_objective_closure"][0]["Objective"], "PO1")


if __name__ == "__main__":
    unittest.main()
