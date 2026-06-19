from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.exit_codes import EXIT_SECURITY_POLICY_VIOLATION
from runtime.init_workflow import LAYOUT_CANONICAL_V16, init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.read_model_requests import request_read_model_rebuild
from runtime.read_models import _propagate_run_graph_metadata, rebuild_read_models
from runtime.reconciliation import run_reconciler
from runtime.schema_validation import validate_project_schemas
from runtime.scheduler import append_event, load_scheduler_snapshot, select_next_action, run_scheduler
from runtime.validation import run_validator
from tests.test_human_summaries import configure_fake_summary_agent
from tests.test_validation import write_plan, write_worker_run


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_hashes(project: Path, paths: list[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        hashes[path.relative_to(project).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return hashes


def write_approval_required_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Approval Fixture

- [ ] T001: Run risky task
  - acceptance: Risky task acceptance.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: high
  - validation: human_approval: requires operator approval
  - max_attempts: 3
  - approval: required
  - deliverables: risky output.
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def write_ready_plan_draft(project: Path, paths: WorkflowPaths, workflow_id: str) -> Path:
    draft_path = paths.planning_dir / "PLAN_DRAFT.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow_id}
- workflow_title: Draft Ready Workflow
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Draft Checklist

- [ ] T001: Execute draft-visible task
  - acceptance: Draft-visible task acceptance.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt
  - max_attempts: 2
  - approval: none
  - deliverables: draft-visible output.
""",
        encoding="utf-8",
    )
    (paths.planning_dir / "plan_readiness_report.json").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "workflow_id": workflow_id,
                "run_id": "plan_fixture",
                "generated_at": "2026-06-11T00:00:00Z",
                "status": "ready_for_activation",
                "ready": True,
                "ready_for_audit": True,
                "ready_for_activation": True,
                "activation_blocked_by": [],
                "summary": {"phases": 1, "tasks": 1, "high_risk_tasks": 0, "requires_human_approval": False},
                "plan_draft_path": draft_path.as_posix(),
                "blocking_questions": [],
                "warnings": [],
                "errors": [],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return draft_path


class ReadModelBuilderIntegrationTest(unittest.TestCase):
    def test_run_graph_metadata_propagates_phase_objective_context_to_run_node(self) -> None:
        nodes = {
            "node_objective_verifier_run_1": {
                "node_id": "node_objective_verifier_run_1",
                "type": "objective_verifier",
                "run_id": "run_1",
                "status": "applied",
            },
            "node_event_1": {
                "node_id": "node_event_1",
                "type": "event",
                "run_id": "run_1",
                "phase_id": "P2",
                "phase": "P2",
                "objective_phase_id": "P2",
                "objective_scope": "phase",
                "target_objective_ids": ["P2.O1"],
            },
        }

        _propagate_run_graph_metadata(nodes)

        run_node = nodes["node_objective_verifier_run_1"]
        self.assertEqual(run_node["phase_id"], "P2")
        self.assertEqual(run_node["objective_phase_id"], "P2")
        self.assertEqual(run_node["objective_scope"], "phase")
        self.assertEqual(run_node["target_objective_ids"], ["P2.O1"])

    def test_rebuild_read_models_smoke_for_minimal_initialized_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Minimal read-model smoke.")
            authoritative_paths = [
                project / "PLAN.md",
                project / ".loopplane" / "runtime" / "state.json",
                project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl",
            ]
            before = file_hashes(project, authoritative_paths)

            result = rebuild_read_models(project)

            after = file_hashes(project, authoritative_paths)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "rebuilt")
            self.assertEqual(after, before)
            read_models_dir = project / ".loopplane" / "read_models"
            self.assertTrue((read_models_dir / "workflow_status.json").is_file())
            workflow_status = json.loads((read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["status"], "initialized")
            self.assertEqual(workflow_status["progress"]["total_tasks"], 0)
            self.assertEqual(workflow_status["completion_marker"]["exists"], False)
            plan_index = json.loads((read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_index["summary"]["total"], 0)

    def test_duplicate_pending_read_model_rebuild_requests_are_coalesced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Read-model request coalesce smoke.")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)

            first = request_read_model_rebuild(
                paths,
                workflow_id=workflow["workflow_id"],
                run_id="run_fixture",
                reason="unit_test",
                requested_by="test",
                source_path=paths.plan_file,
            )
            second = request_read_model_rebuild(
                paths,
                workflow_id=workflow["workflow_id"],
                run_id="run_fixture",
                reason="unit_test",
                requested_by="test",
                source_path=paths.plan_file,
            )

            self.assertEqual(first["request_id"], second["request_id"])
            self.assertTrue(second["coalesced"])
            self.assertEqual(second["coalesced_count"], 2)
            events = read_jsonl(paths.runtime_dir / "events" / "events_000001.jsonl")
            self.assertEqual(
                sum(1 for event in events if event["event_type"] == "read_model_rebuild_requested"),
                1,
            )

    def test_different_pending_read_model_rebuild_requests_share_one_workflow_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Read-model workflow coalesce smoke.")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)

            first = request_read_model_rebuild(
                paths,
                workflow_id=workflow["workflow_id"],
                run_id="run_objective",
                reason="objective_verification_applied",
                requested_by="objective_verifier",
                source_path=paths.plan_file,
            )
            second = request_read_model_rebuild(
                paths,
                workflow_id=workflow["workflow_id"],
                run_id="run_expansion",
                reason="self_expansion_plan_patch_applied",
                requested_by="self_expansion",
                source_path=paths.runtime_dir / "expansions" / "proposal.json",
                extra={"proposal_id": "exp_fixture"},
            )

            self.assertEqual(first["request_id"], second["request_id"])
            self.assertTrue(second["coalesced"])
            self.assertEqual(second["coalesced_scope"], "workflow_pending")
            self.assertEqual(second["coalesced_count"], 2)
            self.assertEqual(second["reason"], "self_expansion_plan_patch_applied")
            self.assertEqual(second["first_reason"], "objective_verification_applied")
            self.assertEqual(
                second["pending_reasons"],
                ["objective_verification_applied", "self_expansion_plan_patch_applied"],
            )
            self.assertEqual(second["latest_run_id"], "run_expansion")
            self.assertEqual(second["pending_extra"]["proposal_id"], "exp_fixture")
            events = read_jsonl(paths.runtime_dir / "events" / "events_000001.jsonl")
            self.assertEqual(
                sum(1 for event in events if event["event_type"] == "read_model_rebuild_requested"),
                1,
            )

    def test_rebuild_derives_objective_expansion_resolution_from_closed_objectives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Read-model objective expansion resolution.")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            paths.plan_file.write_text(
                f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Evidence

- [x] T001: Produce evidence
  - acceptance: Evidence exists.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: report.md
  - max_attempts: 1
  - approval: none
  - deliverables: report.md

### Phase Objective Checklist

- [x] `PO1` Evidence is enough.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Judge evidence completeness.
  - verifier: objective_verifier
  - unmet_action: self_expand
""",
                encoding="utf-8",
            )
            (paths.runtime_dir / "expansion_registry.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": workflow["workflow_id"],
                        "cycle": 1,
                        "events": [],
                        "proposals": [
                            {
                                "proposal_id": "exp_objective_gap",
                                "status": "applied",
                                "expansion_type": "objective_gap",
                                "target_objective_ids": ["PO1"],
                                "objective_resolution_status": "followup_pending",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            workflow_status = json.loads((paths.read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["objective_progress"]["closed"], 1)
            self.assertEqual(workflow_status["self_expansion"]["pending_resolution_count"], 0)
            self.assertEqual(
                workflow_status["self_expansion"]["latest_proposal"]["objective_resolution_status"],
                "resolved",
            )

    def test_rebuild_uses_ready_planning_draft_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Ready draft read-model smoke.")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            draft_path = write_ready_plan_draft(project, paths, str(workflow["workflow_id"]))

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("No active plan has been generated yet", paths.plan_file.read_text(encoding="utf-8"))
            plan_index = json.loads((paths.read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_index["plan_file"], draft_path.relative_to(project).as_posix())
            self.assertEqual(plan_index["active_plan_file"], paths.value("plan_file"))
            self.assertEqual(plan_index["plan_source"]["kind"], "planning_draft")
            self.assertTrue(plan_index["plan_source"]["selected_before_activation"])
            self.assertEqual(plan_index["summary"]["total"], 1)
            self.assertEqual(plan_index["tasks"][0]["task_id"], "T001")
            self.assertEqual(plan_index["phases"][0]["title"], "Phase P0: Draft Checklist")
            self.assertEqual(plan_index["workflow_title"], "Draft Ready Workflow")
            workflow_status = json.loads((paths.read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["progress"]["total_tasks"], 1)
            self.assertEqual(workflow_status["plan_source"]["kind"], "planning_draft")
            self.assertEqual(workflow_status["source_hashes"]["plan_file"], draft_path.relative_to(project).as_posix())

    def test_rebuild_surfaces_active_run_lease_before_worker_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Active lease read-model smoke.")
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            state_path = paths.runtime_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "preparing_run"
            state["scheduler"] = {
                "last_action": "prepare_run",
                "active_run_id": "run_active",
                "active_task_id": "T001",
                "active_role": "worker",
            }
            state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            lease_dir = paths.runtime_dir / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            lease_path = lease_dir / "run_active.json"
            lease_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": workflow["workflow_id"],
                        "run_id": "run_active",
                        "node_id": "node_worker_T001_run_active",
                        "task_id": "T001",
                        "role": "worker",
                        "runner_id": "worker",
                        "status": "running",
                        "prepared_at": "2026-06-11T00:00:00Z",
                        "heartbeat_at": "2026-06-11T00:00:05Z",
                        "active_run_lease_path": ".loopplane/runtime/active_run_leases/run_active.json",
                        "scheduler_run_dir": ".loopplane/runtime/runs/run_active",
                        "role_output_dir": ".loopplane/results/T001/runs/run_active",
                        "prompt_path": ".loopplane/runtime/runs/run_active/prompt.md",
                        "stdout_path": ".loopplane/runtime/runs/run_active/stdout.log",
                        "stderr_path": ".loopplane/runtime/runs/run_active/stderr.log",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            read_models_dir = paths.read_models_dir
            plan_index = json.loads((read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_index["tasks"][0]["deliverables"], "artifacts/result.txt.")
            self.assertEqual(plan_index["workflow_title"], "Validation Fixture Workflow")
            workflow_status = json.loads((read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["workflow_title"], "Validation Fixture Workflow")
            self.assertEqual(workflow_status["status"], "running")
            self.assertEqual(workflow_status["raw_runtime_status"], "preparing_run")
            self.assertEqual(workflow_status["status_source"], "active_run_lease")
            self.assertEqual(workflow_status["active_task_id"], "T001")
            self.assertEqual(workflow_status["active_run_id"], "run_active")
            graph = json.loads((read_models_dir / "workflow_graph.json").read_text(encoding="utf-8"))
            active_nodes = [node for node in graph["nodes"] if node.get("run_id") == "run_active"]
            self.assertEqual(len(active_nodes), 1)
            self.assertEqual(active_nodes[0]["status"], "running")
            self.assertEqual(active_nodes[0]["task_id"], "T001")
            self.assertEqual(active_nodes[0]["deliverables"], "artifacts/result.txt.")
            self.assertTrue(graph["source_hashes"]["active_leases"].startswith("sha256:"))
            run_summaries = read_jsonl(read_models_dir / "run_summaries.jsonl")
            active_runs = [record for record in run_summaries if record.get("run_id") == "run_active"]
            self.assertEqual(len(active_runs), 1)
            self.assertTrue(active_runs[0]["active"])
            self.assertEqual(active_runs[0]["status"], "running")
            sections = {section["key"]: section for section in active_runs[0]["details"]["sections"]}
            self.assertTrue(sections["logs"]["available"])
            log_paths = [item["path"] for item in sections["logs"]["items"]]
            self.assertIn(".loopplane/runtime/runs/run_active/stdout.log", log_paths)
            stdout_log = next(item for item in sections["logs"]["items"] if item["path"].endswith("stdout.log"))
            self.assertTrue(stdout_log["pending"])
            self.assertEqual(stdout_log["size_bytes"], 0)

    def test_cli_rebuilds_read_models_from_plan_latest_validations_events_and_git_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Rebuild read models.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
            run_dir = write_worker_run(project, create_artifact=True)
            validation = run_validator(project, task_id="T001", run_dir=run_dir)
            reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
            self.assertEqual(validation["status"], "pass")
            self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))

            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)
            append_event(
                paths,
                workflow_id=workflow_config["workflow_id"],
                event_type="snapshot_boundary",
                data={"task_id": "T001"},
                snapshot_interval=1,
            )
            tail = append_event(
                paths,
                workflow_id=workflow_config["workflow_id"],
                event_type="tail_after_snapshot",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )

            authoritative_paths = [
                project / "PLAN.md",
                project / ".loopplane" / "runtime" / "state.json",
                project / ".loopplane" / "runtime" / "events" / "events_000001.jsonl",
                project / ".loopplane" / "results" / "T001" / "latest.json",
                run_dir / "validation.json",
            ]
            before = file_hashes(project, authoritative_paths)

            completed = subprocess.run(
                [sys.executable, str(LoopPlane), "rebuild-read-models", "--project", str(project), "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            after = file_hashes(project, authoritative_paths)
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertEqual(after, before)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "rebuilt")
            self.assertEqual(payload["event_replay"]["events_replayed"], 1)

            read_models_dir = project / ".loopplane" / "read_models"
            expected_files = {
                "workflow_status.json",
                "plan_index.json",
                "workflow_graph.json",
                "dashboard_feed.jsonl",
                "run_summaries.jsonl",
                "metrics.json",
                "version_control_status.json",
            }
            self.assertEqual(expected_files, {path.name for path in read_models_dir.iterdir() if path.is_file()})

            workflow_status = json.loads((read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["workflow_id"], workflow_config["workflow_id"])
            self.assertEqual(workflow_status["progress"]["completed_tasks"], 1)
            self.assertEqual(workflow_status["last_event_seq"], tail["sequence"])
            self.assertEqual(workflow_status["source_event_id"], tail["event_id"])
            self.assertIn("source_hashes", workflow_status)
            for filename in expected_files:
                if not filename.endswith(".json"):
                    continue
                payload = json.loads((read_models_dir / filename).read_text(encoding="utf-8"))
                self.assertIn("generated_at", payload, filename)
                self.assertIn("source_hashes", payload, filename)
                self.assertIn("last_event_seq", payload, filename)
                self.assertIn("source_event_id", payload, filename)

            plan_index = json.loads((read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            self.assertEqual(plan_index["summary"]["done"], 1)
            self.assertTrue(plan_index["tasks"])
            task = plan_index["phases"][0]["tasks"][0]
            self.assertEqual(task["task_id"], "T001")
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["latest_run_id"], run_dir.name)
            self.assertEqual(task["validation_status"], "pass")
            self.assertEqual(task["human_summary"]["status"], "ready")
            self.assertIn("Strategic Progress Reading", task["human_summary"]["content"])
            self.assertIn("clearer project asset", task["human_summary"]["content"])
            self.assertEqual(plan_index["phases"][0]["human_summary"]["status"], "ready")
            self.assertIn("project-facing review layer", plan_index["phases"][0]["human_summary"]["content"])

            phase_summary_path = paths.results_dir / "phases" / "P0" / "human_summary.json"
            phase_summary = json.loads(phase_summary_path.read_text(encoding="utf-8"))
            phase_summary.setdefault("source_hashes", {})["summary_source"] = "sha256:stale"
            phase_summary_path.write_text(json.dumps(phase_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            stale_rebuild = rebuild_read_models(project)
            self.assertTrue(stale_rebuild["ok"], json.dumps(stale_rebuild, indent=2, sort_keys=True))
            stale_plan_index = json.loads((read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            stale_phase_summary = stale_plan_index["phases"][0]["human_summary"]
            self.assertEqual(stale_phase_summary["status"], "stale")
            self.assertEqual(stale_phase_summary["stale_reason"], "summary_source_changed")
            self.assertNotIn("content", stale_phase_summary)

            graph = json.loads((read_models_dir / "workflow_graph.json").read_text(encoding="utf-8"))
            self.assertTrue(any(node["type"] == "validation" for node in graph["nodes"]))
            self.assertTrue(any(edge["type"] == "validated_by" for edge in graph["edges"]))

            feed_events = [record["event"] for record in read_jsonl(read_models_dir / "dashboard_feed.jsonl")]
            self.assertIn("validation_passed", feed_events)
            self.assertIn("tail_after_snapshot", feed_events)
            for record in read_jsonl(read_models_dir / "dashboard_feed.jsonl"):
                self.assertIn("generated_at", record)
                self.assertIn("source_hashes", record)
                self.assertIn("source_event_id", record)
                self.assertIn("source_event_seq", record)

            run_summaries = read_jsonl(read_models_dir / "run_summaries.jsonl")
            self.assertTrue(any(record["run_id"] == run_dir.name for record in run_summaries))
            for record in run_summaries:
                self.assertIn("generated_at", record)
                self.assertIn("source_hashes", record)
                self.assertIn("source_event_id", record)
                self.assertIn("source_event_seq", record)

            metrics = json.loads((read_models_dir / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["counts"]["tasks_done"], 1)
            self.assertGreaterEqual(metrics["counts"]["runs_total"], 1)
            self.assertEqual(metrics["event_replay"]["events_replayed"], 1)

            vc_status = json.loads((read_models_dir / "version_control_status.json").read_text(encoding="utf-8"))
            self.assertEqual(vc_status["provider"], "git")
            self.assertIn("repository", vc_status)
            self.assertIn("source_hashes", vc_status)

            schema_validation = validate_project_schemas(project)
            self.assertTrue(schema_validation["ok"], json.dumps(schema_validation, indent=2, sort_keys=True))

    def test_cli_rebuild_read_models_accepts_workflow_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Selected read-model rebuild.", layout=LAYOUT_CANONICAL_V16)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "rebuild-read-models",
                    "--project",
                    str(project),
                    "--workflow",
                    initialized.workflow_id,
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["workflow_id"], initialized.workflow_id)

    def test_rebuild_surfaces_approval_disabled_attention_after_scheduler_records_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Approval disabled read model.")
            write_approval_required_plan(project)
            action = select_next_action(load_scheduler_snapshot(project))
            self.assertEqual(action["action"], "requires_attention")
            self.assertEqual(action["selected"]["type"], "approval_disabled")

            scheduler_result = run_scheduler(project, max_ticks=1)
            self.assertEqual(scheduler_result["selected_action"]["action"], "requires_attention")
            self.assertEqual(scheduler_result["exit_code"], EXIT_SECURITY_POLICY_VIOLATION)
            self.assertFalse((project / ".loopplane" / "results" / "T001" / "runs").exists())

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            workflow_status = json.loads((project / ".loopplane" / "read_models" / "workflow_status.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow_status["status"], "requires_attention")
            self.assertTrue(workflow_status.get("requires_attention"))
            self.assertEqual(workflow_status["requires_attention"][0]["type"], "approval_disabled")
            self.assertEqual(workflow_status["requires_attention"][0]["task_id"], "T001")


if __name__ == "__main__":
    unittest.main()
