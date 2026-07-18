from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

import runtime.read_models as read_models_module
import runtime.scheduler as scheduler_module
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


def read_run_detail(paths: WorkflowPaths, run_id: str) -> dict[str, object]:
    manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
    entry = next(record for record in manifest["runs"] if record["run_id"] == run_id)
    return json.loads((paths.project_root / entry["path"]).read_text(encoding="utf-8"))


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
            nodes_by_id = {node["node_id"]: node for node in graph["nodes"]}
            self.assertEqual(nodes_by_id["node_phase_P0"]["type"], "phase")
            self.assertEqual(nodes_by_id["node_phase_P0"]["layer"], "plan")
            self.assertFalse(nodes_by_id["node_phase_P0"]["pipeline_visible"])
            self.assertEqual(nodes_by_id["node_task_T001"]["type"], "task")
            self.assertEqual(nodes_by_id["node_task_T001"]["phase_id"], "P0")
            self.assertFalse(nodes_by_id["node_task_T001"]["pipeline_visible"])
            edge_types = {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]}
            self.assertIn(("phase_contains_task", "node_phase_P0", "node_task_T001"), edge_types)
            self.assertIn(("task_has_run", "node_task_T001", "node_worker_T001_run_active"), edge_types)
            self.assertEqual(graph["network"]["schema_version"], "1")
            self.assertEqual(graph["network"]["default_layout"], "temporal_force")
            self.assertEqual(graph["network"]["base_layout"], "cose")
            self.assertIn("temporal_force", graph["network"]["available_layouts"])
            self.assertEqual(graph["network"]["temporal_scale"], "rank")
            self.assertEqual(graph["network"]["temporal_pending"], "end_alpha")
            self.assertIn("plan", graph["network"]["default_visible_layers"])
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
            self.assertNotIn("details", active_runs[0])
            run_detail = read_run_detail(paths, "run_active")
            sections = {section["key"]: section for section in run_detail["details"]["sections"]}
            self.assertTrue(sections["logs"]["available"])
            log_paths = [item["path"] for item in sections["logs"]["items"]]
            self.assertIn(".loopplane/runtime/runs/run_active/stdout.log", log_paths)
            stdout_log = next(item for item in sections["logs"]["items"] if item["path"].endswith("stdout.log"))
            self.assertTrue(stdout_log["pending"])
            self.assertEqual(stdout_log["size_bytes"], 0)

    def test_workflow_graph_assigns_start_times_to_phase_task_and_closed_objective_nodes(self) -> None:
        graph = read_models_module._workflow_graph_model(
            common={
                "schema_version": "1.5",
                "workflow_id": "wf_temporal",
                "generated_at": "2026-06-30T00:00:00Z",
            },
            plan_phases=[
                {
                    "phase_id": "P0",
                    "title": "Phase P0: Research Contract",
                    "tasks": [{"task_id": "P0.T001"}],
                    "objectives": [{"objective_id": "P0.O1"}],
                },
            ],
            events=[],
            event_limit=10,
            node_summaries=[],
            agent_statuses=[],
            validation_records=[],
            active_leases=[],
            task_summaries=[
                {
                    "task_id": "P0.T001",
                    "status": "done",
                    "title": "Establish source-grounded research contract",
                    "phase": "Phase P0: Research Contract",
                    "latest_run_id": "run_20260626_225143_18258b43",
                    "last_updated_at": "2026-06-26T22:55:32Z",
                },
            ],
            objective_model={
                "objectives": [
                    {
                        "objective_id": "P0.O1",
                        "phase_id": "P0",
                        "status": "closed",
                        "text": "Establish the research contract.",
                        "verified_at": "2026-06-27T10:17:44Z",
                    }
                ],
            },
            expansion_registry={},
            event_total_count=0,
        )

        nodes_by_id = {node["node_id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes_by_id["node_task_P0_T001"]["started_at"], "2026-06-26T22:51:43Z")
        self.assertEqual(nodes_by_id["node_task_P0_T001"]["ended_at"], "2026-06-26T22:55:32Z")
        self.assertEqual(nodes_by_id["node_phase_P0"]["started_at"], "2026-06-26T22:51:43Z")
        self.assertEqual(nodes_by_id["node_phase_P0"]["ended_at"], "2026-06-26T22:55:32Z")
        self.assertEqual(nodes_by_id["objective_P0_O1"]["started_at"], "2026-06-27T10:17:44Z")
        self.assertEqual(nodes_by_id["objective_P0_O1"]["ended_at"], "2026-06-27T10:17:44Z")

    def test_workflow_graph_connects_taskless_adapter_runs_from_context(self) -> None:
        graph = read_models_module._workflow_graph_model(
            common={
                "schema_version": "1.5",
                "workflow_id": "wf_context",
                "generated_at": "2026-06-30T00:00:00Z",
            },
            plan_phases=[
                {
                    "phase_id": "P5",
                    "title": "Phase P5",
                    "tasks": [],
                    "objectives": [{"objective_id": "P5.O1"}],
                },
                {
                    "phase_id": "P7",
                    "title": "Phase P7",
                    "tasks": [{"task_id": "P7.T005"}],
                    "objectives": [],
                },
            ],
            events=[
                {
                    "event_type": "objective_verifier_adapter_started",
                    "ts": "2026-06-30T00:00:01Z",
                    "data": {
                        "run_id": "run_obj",
                        "phase_id": "P5",
                        "target_objective_ids": ["P5.O1"],
                    },
                },
                {
                    "event_type": "self_expansion_requires_attention",
                    "ts": "2026-06-30T00:00:02Z",
                    "data": {
                        "run_id": "run_exp",
                        "proposal_id": "exp_20260630_run_exp_p7_t005",
                        "loop_signature": "objective_verifier_phase_P5_repeated_duplicate_stop",
                    },
                },
            ],
            event_limit=1,
            node_summaries=[],
            agent_statuses=[
                {
                    "run_id": "run_obj",
                    "role": "objective_verifier",
                    "status": "completed",
                    "started_at": "2026-06-30T00:00:01Z",
                },
                {
                    "run_id": "run_exp",
                    "role": "expansion_planner",
                    "status": "requires_attention",
                    "started_at": "2026-06-30T00:00:02Z",
                },
            ],
            validation_records=[],
            active_leases=[],
            task_summaries=[
                {
                    "task_id": "P7.T005",
                    "status": "pending",
                    "title": "Expanded task",
                    "phase": "Phase P7",
                },
            ],
            objective_model={
                "objectives": [
                    {
                        "objective_id": "P5.O1",
                        "phase_id": "P5",
                        "status": "open",
                        "text": "Verify P5 objective.",
                    }
                ],
            },
            expansion_registry={
                "proposals": [
                    {
                        "run_id": "run_exp",
                        "proposal_id": "exp_20260630_run_exp_p7_t005",
                        "loop_signature": "objective_verifier_phase_P5_repeated_duplicate_stop",
                        "status": "requires_attention",
                    }
                ]
            },
            event_total_count=2,
        )

        nodes_by_run = {
            node["run_id"]: node
            for node in graph["nodes"]
            if node.get("run_id") in {"run_obj", "run_exp"} and node.get("type") != "event"
        }
        objective_run = nodes_by_run["run_obj"]
        expansion_run = nodes_by_run["run_exp"]
        edge_types = {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]}
        self.assertIn("P5", objective_run["phase_ids"])
        self.assertIn("P5.O1", objective_run["target_objective_ids"])
        self.assertIn(("phase_has_run", "node_phase_P5", objective_run["node_id"]), edge_types)
        self.assertIn(("run_supports_objective", objective_run["node_id"], "objective_P5_O1"), edge_types)
        self.assertIn("P5", expansion_run["phase_ids"])
        self.assertIn("P7", expansion_run["phase_ids"])
        self.assertIn("P7.T005", expansion_run["target_task_ids"])
        self.assertIn(("phase_has_run", "node_phase_P5", expansion_run["node_id"]), edge_types)
        self.assertIn(("phase_has_run", "node_phase_P7", expansion_run["node_id"]), edge_types)
        self.assertIn(("task_has_run", "node_task_P7_T005", expansion_run["node_id"]), edge_types)

    def test_run_context_extracts_nested_adapter_summary_tokens(self) -> None:
        context = read_models_module._run_context_index(
            [
                {
                    "event_type": "expansion_planner_run_classified",
                    "payload": {
                        "run_id": "run_nested",
                        "agent_status": {
                            "summary": "Blocked on the stale P5 objective verifier loop; P7.T004 already records the stop.",
                        },
                    },
                }
            ],
            {},
        )

        self.assertIn("P5", context["run_nested"]["phase_ids"])
        self.assertIn("P7", context["run_nested"]["phase_ids"])
        self.assertIn("P7.T004", context["run_nested"]["target_task_ids"])

    def test_workflow_graph_ignores_rebuild_metadata_when_linking_expansion_runs(self) -> None:
        graph = read_models_module._workflow_graph_model(
            common={
                "schema_version": "1.5",
                "workflow_id": "wf_expansion_context",
                "generated_at": "2026-06-30T00:00:00Z",
            },
            plan_phases=[
                {
                    "phase_id": "P0",
                    "title": "Phase P0",
                    "tasks": [{"task_id": "P0.T001"}],
                    "objectives": [],
                },
                {
                    "phase_id": "P5",
                    "title": "Phase P5",
                    "tasks": [{"task_id": "P5.T012"}, {"task_id": "P5.T013"}],
                    "objectives": [{"objective_id": "P5.O1"}],
                },
            ],
            events=[
                {
                    "event_type": "self_expansion_requires_attention",
                    "ts": "2026-06-30T00:00:01Z",
                    "payload": {
                        "run_id": "run_expand",
                        "proposal": {
                            "proposal_id": "exp_run_expand_p5_o1",
                            "target_objective_ids": ["P5.O1"],
                            "target_task_ids": ["P5.T012"],
                            "added_task_ids": ["P5.T013"],
                            "objective_followup_update": {
                                "read_model_rebuild": {
                                    "first_run_id": "run_20260626_225143_18258b43",
                                    "first_source_path": ".loopplane/results/P0.T001/runs/run_20260626_225143_18258b43/validation.json",
                                }
                            },
                        },
                    },
                }
            ],
            event_limit=10,
            node_summaries=[],
            agent_statuses=[
                {
                    "run_id": "run_expand",
                    "role": "expansion_planner",
                    "status": "applied",
                    "started_at": "2026-06-30T00:00:01Z",
                }
            ],
            validation_records=[],
            active_leases=[],
            task_summaries=[
                {"task_id": "P0.T001", "status": "done", "title": "Contract", "phase": "Phase P0"},
                {"task_id": "P5.T012", "status": "done", "title": "Prior follow-up", "phase": "Phase P5"},
                {"task_id": "P5.T013", "status": "pending", "title": "New follow-up", "phase": "Phase P5"},
            ],
            objective_model={
                "objectives": [
                    {
                        "objective_id": "P5.O1",
                        "phase_id": "P5",
                        "status": "needs_verification",
                        "text": "Verify P5 objective.",
                    }
                ],
            },
            expansion_registry={},
            event_total_count=1,
        )

        expansion_node = next(
            node for node in graph["nodes"] if node.get("run_id") == "run_expand" and node.get("type") == "expansion_planner"
        )
        self.assertEqual(expansion_node.get("phase_ids"), ["P5"])
        self.assertEqual(expansion_node.get("target_task_ids"), ["P5.T012", "P5.T013"])
        edge_types = {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]}
        self.assertIn(("phase_has_run", "node_phase_P5", expansion_node["node_id"]), edge_types)
        self.assertIn(("task_has_run", "node_task_P5_T012", expansion_node["node_id"]), edge_types)
        self.assertIn(("task_has_run", "node_task_P5_T013", expansion_node["node_id"]), edge_types)
        self.assertNotIn(("phase_has_run", "node_phase_P0", expansion_node["node_id"]), edge_types)
        self.assertNotIn(("task_has_run", "node_task_P0_T001", expansion_node["node_id"]), edge_types)

    def test_rebuild_bounds_large_log_and_artifact_previews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Bound large dashboard previews.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            run_dir = write_worker_run(project, create_artifact=True)
            large_text = "large-preview-line\n" * 2048
            (run_dir / "logs" / "stdout.log").write_text(large_text, encoding="utf-8")
            (run_dir / "artifacts" / "large.txt").write_text(large_text, encoding="utf-8")

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            run_summaries = read_jsonl(paths.read_models_dir / "run_summaries.jsonl")
            run_summary = next(record for record in run_summaries if record.get("run_id") == run_dir.name)
            self.assertNotIn("details", run_summary)
            self.assertTrue(run_summary["details_externalized"])
            run_detail = read_run_detail(paths, run_dir.name)
            sections = {section["key"]: section for section in run_detail["details"]["sections"]}

            stdout_log = next(item for item in sections["logs"]["items"] if item["path"].endswith("stdout.log"))
            self.assertEqual(stdout_log["size_bytes"], len(large_text.encode("utf-8")))
            self.assertRegex(str(stdout_log.get("last_written_at")), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertIsInstance(stdout_log.get("mtime_ns"), int)
            self.assertTrue(stdout_log["truncated"])
            self.assertFalse(stdout_log["full_sha256_available"])
            self.assertIn("preview_sha256", stdout_log)
            self.assertNotIn("sha256", stdout_log)

            large_artifact = next(item for item in sections["artifacts"]["items"] if item["path"].endswith("large.txt"))
            self.assertEqual(large_artifact["size_bytes"], len(large_text.encode("utf-8")))
            self.assertTrue(large_artifact["truncated"])
            self.assertFalse(large_artifact["full_sha256_available"])
            self.assertIn("preview_sha256", large_artifact)
            self.assertNotIn("sha256", large_artifact)

    def test_rebuild_limits_persisted_run_details_but_keeps_full_run_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Limit persisted run details.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            for run_id in ("run_a", "run_b", "run_c"):
                write_worker_run(project, run_id=run_id, create_artifact=True)

            original_limit = read_models_module.RUN_DETAIL_BUILD_LIMIT
            read_models_module.RUN_DETAIL_BUILD_LIMIT = 1
            try:
                result = rebuild_read_models(project)
            finally:
                read_models_module.RUN_DETAIL_BUILD_LIMIT = original_limit

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            run_index = read_jsonl(paths.read_models_dir / "run_index.jsonl")
            self.assertEqual({record["run_id"] for record in run_index}, {"run_a", "run_b", "run_c"})
            self.assertEqual(sum(1 for record in run_index if record.get("detail_status") == "available"), 1)
            manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_count"], 1)
            detail_files = sorted((paths.read_models_dir / "run_details").glob("*.json"))
            self.assertEqual(len(detail_files), 1)

    def test_run_index_record_normalizes_fractional_utc_timestamps(self) -> None:
        record = {
            "schema_version": "1.5",
            "workflow_id": "wf_fixture",
            "generated_at": "2026-06-23T01:51:25.888998+00:00",
            "source_hashes": {},
            "run_id": "run_fixture",
            "node_id": "node_worker_fixture",
            "detail_status": "not_built",
            "started_at": "2026-06-23T01:51:25.888998+00:00",
            "ended_at": "2026-06-23T01:54:25.123456Z",
            "details": {"available_sections": [], "missing_sections": []},
        }

        index = read_models_module._run_index_record(object(), record)  # type: ignore[arg-type]

        self.assertEqual(index["generated_at"], "2026-06-23T01:51:25Z")
        self.assertEqual(index["started_at"], "2026-06-23T01:51:25Z")
        self.assertEqual(index["ended_at"], "2026-06-23T01:54:25Z")

    def test_rebuild_uses_fast_event_source_refs_without_full_event_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Fast event source references.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_fast_events", create_artifact=True)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            append_event(
                paths,
                workflow_id=paths.workflow_id,
                event_type="fast_event_source_marker",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )

            original = read_models_module._events_sha256
            original_read_jsonl = read_models_module._read_jsonl

            def fail_full_event_hash(*args: object, **kwargs: object) -> str:
                raise AssertionError("read-model rebuild should use fast event source refs")

            def fail_full_event_jsonl(path: Path, *args: object, **kwargs: object) -> list[dict[str, object]]:
                if path.name.startswith("events_") and path.suffix == ".jsonl":
                    raise AssertionError("read-model rebuild should read bounded event tails")
                return original_read_jsonl(path, *args, **kwargs)

            read_models_module._events_sha256 = fail_full_event_hash
            read_models_module._read_jsonl = fail_full_event_jsonl
            try:
                result = rebuild_read_models(project)
            finally:
                read_models_module._events_sha256 = original
                read_models_module._read_jsonl = original_read_jsonl

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            workflow_status = json.loads((paths.read_models_dir / "workflow_status.json").read_text(encoding="utf-8"))
            source_hashes = workflow_status["source_hashes"]
            self.assertEqual(source_hashes["events_source_mode"], "event_manifest")
            self.assertIn("events_segment_manifest", source_hashes)
            self.assertNotIn("events_sha256", source_hashes)

    def test_strict_read_model_diagnostics_hashes_events_and_verifies_payload_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Strict read-model diagnostics.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_strict", create_artifact=True)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            original_threshold = scheduler_module.EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES
            scheduler_module.EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES = 128
            try:
                append_event(
                    paths,
                    workflow_id=paths.workflow_id,
                    event_type="strict_diagnostics_large_payload",
                    data={"task_id": "T001", "notes": "x" * 512},
                    snapshot_interval=None,
                )
            finally:
                scheduler_module.EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES = original_threshold
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))

            diagnostics = read_models_module.strict_read_model_diagnostics(project)

            self.assertTrue(diagnostics["ok"], json.dumps(diagnostics, indent=2, sort_keys=True))
            self.assertEqual(diagnostics["checks"]["events"]["chain"]["status"], "pass")
            self.assertTrue(str(diagnostics["checks"]["events"]["events_sha256"]).startswith("sha256:"))
            self.assertEqual(diagnostics["checks"]["payload_sidecars"]["referenced"], 1)
            self.assertEqual(diagnostics["checks"]["payload_sidecars"]["checked"], 1)
            self.assertEqual(diagnostics["checks"]["payload_sidecars"]["mismatch"], 0)

            sidecar_path = next((paths.runtime_dir / scheduler_module.EVENT_PAYLOAD_SIDECAR_DIR).glob("*.json"))
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["payload"]["notes"] = "tampered"
            sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            broken = read_models_module.strict_read_model_diagnostics(project)

            self.assertFalse(broken["ok"])
            self.assertEqual(broken["checks"]["payload_sidecars"]["status"], "fail")
            self.assertEqual(broken["checks"]["payload_sidecars"]["mismatch"], 1)
            self.assertIn("event payload sidecar integrity check failed", broken["errors"])

    def test_strict_read_model_diagnostics_detects_missing_split_run_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Strict split run detail diagnostics.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_detail_missing", create_artifact=True)
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            detail_path = project / manifest["runs"][0]["path"]
            detail_path.unlink()

            diagnostics = read_models_module.strict_read_model_diagnostics(project)

            self.assertFalse(diagnostics["ok"])
            self.assertEqual(diagnostics["checks"]["run_details"]["status"], "fail")
            self.assertEqual(diagnostics["checks"]["run_details"]["missing"], 1)
            self.assertIn("run detail manifest integrity check failed", diagnostics["errors"])

    def test_no_write_rebuild_comparison_detects_changed_read_model_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "No-write rebuild comparison.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_compare", create_artifact=True)
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            workflow_status_path = paths.read_models_dir / "workflow_status.json"
            workflow_status = json.loads(workflow_status_path.read_text(encoding="utf-8"))
            workflow_status["status"] = "tampered"
            workflow_status_path.write_text(json.dumps(workflow_status, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            compared = rebuild_read_models(project, write=False)

            self.assertTrue(compared["ok"], json.dumps(compared, indent=2, sort_keys=True))
            comparison = compared["no_write_comparison"]
            self.assertEqual(comparison["status"], "fail")
            self.assertIn("workflow_status.json", comparison["changed_files"])
            self.assertEqual(compared["written_files"], [])

    def test_rebuild_reuses_unchanged_run_details_from_build_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Reuse run detail build manifest.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            run_dir = write_worker_run(project, run_id="run_reuse", create_artifact=True)
            first = rebuild_read_models(project)
            self.assertTrue(first["ok"], json.dumps(first, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            detail_path = next((paths.read_models_dir / "run_details").glob("*.json"))
            detail_mtime_ns = detail_path.stat().st_mtime_ns
            stale_detail_path = paths.read_models_dir / "run_details" / "stale_orphan.json"
            stale_detail_path.write_text('{"schema_version": "1.5", "run_id": "stale"}\n', encoding="utf-8")

            original = read_models_module._run_detail_sections

            def fail_if_rebuilt(*args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("unchanged run detail should be reused")

            read_models_module._run_detail_sections = fail_if_rebuilt
            try:
                second = rebuild_read_models(project)
            finally:
                read_models_module._run_detail_sections = original

            self.assertTrue(second["ok"], json.dumps(second, indent=2, sort_keys=True))
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_records_reused"], 1)
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_files_reused_on_disk"], 1)
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_files_written"], 0)
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_files_removed"], 1)
            self.assertEqual(detail_path.stat().st_mtime_ns, detail_mtime_ns)
            self.assertFalse(any(str(item).endswith(detail_path.name) for item in second["written_files"]))
            self.assertFalse(stale_detail_path.exists())
            build_manifest = json.loads((paths.read_models_dir / "build_manifest.json").read_text(encoding="utf-8"))
            detail_entry = next(record for record in build_manifest["run_details"] if record["run_id"] == "run_reuse")
            self.assertTrue(detail_entry["detail_reused"])
            workflow_status_path = paths.read_models_dir / "workflow_status.json"
            workflow_status_mtime_ns = workflow_status_path.stat().st_mtime_ns
            stable = rebuild_read_models(project)
            self.assertTrue(stable["ok"], json.dumps(stable, indent=2, sort_keys=True))
            self.assertEqual(stable["written_files"], [])
            self.assertEqual(stable["diagnostics"]["counts"]["written_files"], 0)
            self.assertEqual(stable["diagnostics"]["counts"]["read_model_files_written"], 0)
            self.assertEqual(stable["diagnostics"]["counts"]["read_model_files_unchanged"], len(read_models_module.READ_MODEL_FILES))
            self.assertEqual(stable["diagnostics"]["counts"]["run_detail_files_reused_on_disk"], 1)
            self.assertEqual(workflow_status_path.stat().st_mtime_ns, workflow_status_mtime_ns)

            (run_dir / "prompt.md").write_text("# Updated Prompt\n\nThis should invalidate the detail source.\n", encoding="utf-8")
            calls = {"count": 0}

            def count_rebuild(*args: object, **kwargs: object) -> dict[str, object]:
                calls["count"] += 1
                return original(*args, **kwargs)

            read_models_module._run_detail_sections = count_rebuild
            try:
                third = rebuild_read_models(project)
            finally:
                read_models_module._run_detail_sections = original

            self.assertTrue(third["ok"], json.dumps(third, indent=2, sort_keys=True))
            self.assertEqual(calls["count"], 1)
            self.assertEqual(third["diagnostics"]["counts"]["run_detail_records_reused"], 0)
            self.assertEqual(third["diagnostics"]["counts"]["run_detail_files_written"], 1)

    def test_rebuild_refreshes_reused_run_detail_envelope_after_event_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Refresh reused run detail envelope.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_reuse_event", create_artifact=True)
            first = rebuild_read_models(project)
            self.assertTrue(first["ok"], json.dumps(first, indent=2, sort_keys=True))
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            detail_path = next((paths.read_models_dir / "run_details").glob("*.json"))
            first_detail = json.loads(detail_path.read_text(encoding="utf-8"))

            event = append_event(
                paths,
                workflow_id=workflow["workflow_id"],
                event_type="read_model_test_event",
                data={"source": "run_detail_envelope_regression"},
                snapshot_interval=None,
            )
            second = rebuild_read_models(project)

            self.assertTrue(second["ok"], json.dumps(second, indent=2, sort_keys=True))
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_records_reused"], 1)
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_files_written"], 1)
            self.assertEqual(second["diagnostics"]["counts"]["run_detail_files_reused_on_disk"], 0)
            second_detail = json.loads(detail_path.read_text(encoding="utf-8"))
            self.assertNotEqual(second_detail["source_event_seq"], first_detail["source_event_seq"])
            self.assertEqual(second_detail["source_event_id"], event["event_id"])
            strict = read_models_module.strict_read_model_diagnostics(project, compare_rebuild=True)
            self.assertTrue(strict["ok"], json.dumps(strict, indent=2, sort_keys=True))

    def test_rebuild_write_path_uses_single_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Read-model rebuild write lock.")
            configure_fake_summary_agent(project)
            write_plan(project, validation="file_exists: artifacts/result.txt")
            write_worker_run(project, run_id="run_locked", create_artifact=True)
            first = rebuild_read_models(project)
            self.assertTrue(first["ok"], json.dumps(first, indent=2, sort_keys=True))
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            protected = [
                *(paths.read_models_dir / filename for filename in read_models_module.READ_MODEL_FILES),
                *sorted((paths.read_models_dir / "run_details").glob("*.json")),
            ]
            before = file_hashes(project, protected)
            lock = read_models_module.AtomicOwnerLock(
                paths.runtime_dir / "lock" / "read_model_rebuild_lock",
                "test-read-model-lock-holder",
                ttl_seconds=60,
            )
            original_wait = read_models_module.READ_MODEL_REBUILD_LOCK_WAIT_SECONDS
            read_models_module.READ_MODEL_REBUILD_LOCK_WAIT_SECONDS = 0
            try:
                with lock.acquire():
                    locked = rebuild_read_models(project)
            finally:
                read_models_module.READ_MODEL_REBUILD_LOCK_WAIT_SECONDS = original_wait

            after = file_hashes(project, protected)
            self.assertFalse(locked["ok"])
            self.assertEqual(locked["status"], "read_model_rebuild_locked")
            self.assertEqual(locked["written_files"], [])
            self.assertEqual(after, before)
            self.assertFalse(locked["diagnostics"]["counts"]["write_lock_acquired"])

    def test_self_expansion_graph_nodes_are_aggregated_beyond_detail_limit(self) -> None:
        nodes = [
            {
                "node_id": f"node_event_{index}",
                "type": "event",
                "status": "expansion_planner_run_classified",
                "event_type": "expansion_planner_run_classified",
                "event_sequence": index,
                "started_at": f"2026-06-11T00:0{index}:00Z",
                "run_id": f"run_expansion_{index}",
                "summary": {"one_line": "expansion", "highlights": [], "risks": []},
            }
            for index in range(1, 5)
        ]
        nodes.append(
            {
                "node_id": "node_worker",
                "type": "worker",
                "status": "pass",
                "started_at": "2026-06-11T00:05:00Z",
                "run_id": "run_worker",
                "summary": {"one_line": "worker", "highlights": [], "risks": []},
            }
        )
        edges = [
            {"edge_id": f"edge_{index}", "source": f"node_event_{index}", "target": f"node_event_{index + 1}", "type": "event_sequence"}
            for index in range(1, 4)
        ]

        graph = read_models_module._aggregate_self_expansion_graph(nodes, edges, detail_limit=2)

        aggregation = graph["aggregation"]
        self.assertTrue(aggregation["enabled"])
        self.assertEqual(aggregation["aggregated_node_count"], 2)
        visible_ids = {node["node_id"] for node in graph["nodes"]}
        self.assertIn("node_worker", visible_ids)
        self.assertIn("node_event_3", visible_ids)
        self.assertIn("node_event_4", visible_ids)
        aggregate_ids = {node_id for node_id in visible_ids if node_id.startswith("aggregate_self_expansion_")}
        self.assertEqual(len(aggregate_ids), 1)
        aggregate_node = next(node for node in graph["nodes"] if node["node_id"] in aggregate_ids)
        self.assertEqual(aggregate_node["aggregated_node_count"], 2)
        self.assertEqual(aggregate_node["retention_policy"], "recent_active_detail_with_historical_buckets")
        self.assertEqual(aggregate_node["time_bucket"], "2026-06-11")
        self.assertEqual(aggregate_node["expansion_type"], "unknown")
        self.assertEqual(aggregate_node["target_key"], "workflow")
        self.assertEqual(aggregate_node["sample_run_ids"], ["run_expansion_1", "run_expansion_2"])
        self.assertEqual(aggregation["retention_policy"]["mode"], "recent_active_detail_with_historical_buckets")
        self.assertEqual(
            aggregation["retention_policy"]["bucket_fields"],
            ["time_bucket", "event_type", "expansion_type", "terminal_status", "target_key"],
        )
        self.assertEqual(aggregation["groups"][0]["time_bucket"], "2026-06-11")
        self.assertEqual(aggregation["groups"][0]["target_key"], "workflow")
        self.assertEqual(aggregation["visible_node_count"], len(graph["nodes"]))
        self.assertTrue(any(edge.get("aggregated") for edge in graph["edges"]))

    def test_self_expansion_aggregation_keeps_distinct_targets_and_types_separate(self) -> None:
        nodes = [
            {
                "node_id": f"node_expansion_{index}",
                "type": "event",
                "status": "self_expansion_plan_patch_applied",
                "event_type": "self_expansion_plan_patch_applied",
                "started_at": "2026-06-11T00:00:00Z",
                "run_id": f"run_expansion_{index}",
                "expansion_type": expansion_type,
                "target_failure_ids": [target_failure_id],
                "summary": {"one_line": "expansion", "highlights": [], "risks": []},
            }
            for index, (expansion_type, target_failure_id) in enumerate(
                [
                    ("objective_gap", "failure_a"),
                    ("objective_gap", "failure_a"),
                    ("missing_deliverable", "failure_b"),
                    ("missing_deliverable", "failure_b"),
                ],
                start=1,
            )
        ]

        graph = read_models_module._aggregate_self_expansion_graph(nodes, [], detail_limit=0)

        aggregation = graph["aggregation"]
        self.assertTrue(aggregation["enabled"])
        self.assertEqual(aggregation["aggregated_node_count"], 4)
        groups = sorted(aggregation["groups"], key=lambda group: group["expansion_type"])
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["expansion_type"], "missing_deliverable")
        self.assertEqual(groups[0]["target_key"], "target_failure:failure_b")
        self.assertEqual(groups[1]["expansion_type"], "objective_gap")
        self.assertEqual(groups[1]["target_key"], "target_failure:failure_a")

    def test_objective_verifier_graph_nodes_are_aggregated_with_collapsed_edges(self) -> None:
        nodes = [
            {
                "node_id": "node_phase_P5",
                "type": "phase",
                "layer": "plan",
                "status": "completed",
                "title": "Phase P5",
            },
            {
                "node_id": "objective_P5_O1",
                "type": "objective",
                "layer": "objective",
                "status": "open",
                "title": "Objective P5.O1",
            },
        ]
        for index in range(1, 6):
            nodes.append(
                {
                    "node_id": f"node_objective_verifier_run_{index}",
                    "type": "objective_verifier",
                    "status": "completed",
                    "started_at": f"2026-06-11T00:0{index}:00Z",
                    "run_id": f"run_verifier_{index}",
                    "phase_ids": ["P5"],
                    "target_objective_ids": ["P5.O1"],
                    "summary": {"one_line": "verifier", "highlights": [], "risks": []},
                }
            )
        edges = []
        for index in range(1, 6):
            verifier_id = f"node_objective_verifier_run_{index}"
            edges.extend(
                [
                    {
                        "edge_id": f"edge_phase_{index}",
                        "source": "node_phase_P5",
                        "target": verifier_id,
                        "type": "phase_has_run",
                        "label": "run",
                        "layer": "runtime",
                    },
                    {
                        "edge_id": f"edge_objective_{index}",
                        "source": verifier_id,
                        "target": "objective_P5_O1",
                        "type": "run_supports_objective",
                        "label": "supports",
                        "layer": "objective",
                    },
                ]
            )

        graph = read_models_module._aggregate_objective_verifier_graph(nodes, edges, detail_limit=2)

        aggregation = graph["aggregation"]
        self.assertTrue(aggregation["enabled"])
        self.assertEqual(aggregation["aggregated_node_count"], 3)
        visible_ids = {node["node_id"] for node in graph["nodes"]}
        self.assertIn("node_objective_verifier_run_4", visible_ids)
        self.assertIn("node_objective_verifier_run_5", visible_ids)
        aggregate_ids = {node_id for node_id in visible_ids if node_id.startswith("aggregate_objective_verifier_")}
        self.assertEqual(len(aggregate_ids), 1)
        aggregate_id = next(iter(aggregate_ids))
        aggregate_node = next(node for node in graph["nodes"] if node["node_id"] == aggregate_id)
        self.assertEqual(aggregate_node["aggregated_node_count"], 3)
        self.assertEqual(aggregate_node["phase_ids"], ["P5"])
        self.assertEqual(aggregate_node["target_objective_ids"], ["P5.O1"])
        aggregate_edges = [edge for edge in graph["edges"] if edge.get("source") == aggregate_id or edge.get("target") == aggregate_id]
        self.assertEqual(len(aggregate_edges), 2)
        self.assertTrue(all(edge.get("aggregated") for edge in aggregate_edges))
        self.assertEqual({edge.get("aggregated_edge_count") for edge in aggregate_edges}, {3})
        self.assertIn(("phase_has_run", "node_phase_P5", aggregate_id), {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]})
        self.assertIn(("run_supports_objective", aggregate_id, "objective_P5_O1"), {(edge["type"], edge["source"], edge["target"]) for edge in graph["edges"]})

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
            diagnostics = payload["diagnostics"]
            self.assertGreaterEqual(diagnostics["total_duration_ms"], 0)
            span_names = {span["name"] for span in diagnostics["spans"]}
            self.assertIn("plan_parsing", span_names)
            self.assertIn("event_read", span_names)
            self.assertIn("event_projection", span_names)
            self.assertIn("run_summary_construction", span_names)
            self.assertIn("workflow_graph_model", span_names)
            self.assertIn("schema_validation", span_names)
            self.assertIn("writes", span_names)
            self.assertGreaterEqual(diagnostics["counts"]["events_loaded"], 1)
            self.assertGreaterEqual(diagnostics["counts"]["event_segment_bytes"], 1)

            read_models_dir = project / ".loopplane" / "read_models"
            expected_files = {
                "workflow_status.json",
                "plan_index.json",
                "workflow_graph.json",
                "dashboard_feed.jsonl",
                "run_index.jsonl",
                "run_summaries.jsonl",
                "metrics.json",
                "version_control_status.json",
                "run_details_manifest.json",
                "build_manifest.json",
            }
            self.assertEqual(expected_files, {path.name for path in read_models_dir.iterdir() if path.is_file()})

            strict_paths = [
                *authoritative_paths,
                *(read_models_dir / filename for filename in expected_files),
            ]
            strict_before = file_hashes(project, strict_paths)
            strict_completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "rebuild-read-models",
                    "--project",
                    str(project),
                    "--strict-diagnostics",
                    "--compare-rebuild",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            strict_after = file_hashes(project, strict_paths)
            self.assertEqual(strict_completed.returncode, 0, strict_completed.stderr + strict_completed.stdout)
            self.assertEqual(strict_after, strict_before)
            strict_payload = json.loads(strict_completed.stdout)
            self.assertTrue(strict_payload["ok"], json.dumps(strict_payload, indent=2, sort_keys=True))
            self.assertTrue(strict_payload["strict"])
            self.assertEqual(strict_payload["status"], "pass")
            self.assertTrue(str(strict_payload["checks"]["events"]["events_sha256"]).startswith("sha256:"))
            self.assertEqual(strict_payload["checks"]["events"]["chain"]["status"], "pass")
            self.assertEqual(strict_payload["checks"]["run_details"]["status"], "pass")
            self.assertEqual(strict_payload["checks"]["no_write_rebuild_comparison"]["status"], "pass")
            self.assertEqual(strict_payload["checks"]["no_write_rebuild_comparison"]["comparison"]["changed_files"], [])

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
                self.assertNotIn("details", record)
            run_index = read_jsonl(read_models_dir / "run_index.jsonl")
            self.assertTrue(any(record["run_id"] == run_dir.name for record in run_index))
            self.assertTrue(all("details" not in record for record in run_index))
            detail_manifest = json.loads((read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            detail_entry = next(record for record in detail_manifest["runs"] if record["run_id"] == run_dir.name)
            self.assertTrue((project / detail_entry["path"]).is_file())
            build_manifest = json.loads((read_models_dir / "build_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(build_manifest["workflow_id"], workflow_config["workflow_id"])
            self.assertTrue(build_manifest["builder_version"])
            self.assertTrue(build_manifest["run_details"])

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
            self.assertIn(".loopplane/read_models/run_index.jsonl", schema_validation["checked_files"])
            self.assertIn(".loopplane/read_models/run_details_manifest.json", schema_validation["checked_files"])
            self.assertIn(".loopplane/read_models/build_manifest.json", schema_validation["checked_files"])
            self.assertTrue(
                any(path.startswith(".loopplane/read_models/run_details/") for path in schema_validation["checked_files"])
            )
            self.assertIn("read_model_build_manifest.schema.json", schema_validation["schemas_used"])
            self.assertIn("read_model_run_index.schema.json", schema_validation["schemas_used"])
            self.assertIn("read_model_run_detail.schema.json", schema_validation["schemas_used"])
            self.assertIn("read_model_run_details_manifest.schema.json", schema_validation["schemas_used"])

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

    def test_cli_migrate_splits_legacy_run_summaries_without_deleting_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Split legacy run summaries.", layout=LAYOUT_CANONICAL_V16)
            workflow_config = load_workflow_config(project, workflow_id=initialized.workflow_id)
            paths = WorkflowPaths.from_config(project, workflow_config)
            paths.read_models_dir.mkdir(parents=True, exist_ok=True)
            legacy_path = paths.read_models_dir / "run_summaries.jsonl"
            legacy_record = {
                "schema_version": "1.5",
                "workflow_id": initialized.workflow_id,
                "generated_at": "2026-06-24T00:00:00Z",
                "source_hashes": {"legacy": "sha256:abc"},
                "source_event_seq": 3,
                "source_event_id": "event_legacy",
                "run_id": "run_legacy",
                "node_id": "node_run_legacy",
                "task_id": "T001",
                "role": "worker",
                "status": "pass",
                "summary": {"status": "pass"},
                "details": {
                    "schema_version": "1.5",
                    "run_id": "run_legacy",
                    "task_id": "T001",
                    "sections": [{"key": "agent_status", "title": "Agent Status", "content": "ok"}],
                    "available_sections": ["agent_status"],
                    "missing_sections": [],
                },
            }
            legacy_path.write_text(json.dumps(legacy_record, sort_keys=True) + "\n", encoding="utf-8")

            dry_run = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "migrate",
                    "--project",
                    str(project),
                    "--workflow",
                    initialized.workflow_id,
                    "--split-run-summaries",
                    "--dry-run",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(dry_run.returncode, 0, dry_run.stderr + dry_run.stdout)
            dry_payload = json.loads(dry_run.stdout)
            self.assertTrue(dry_payload["ok"], json.dumps(dry_payload, indent=2, sort_keys=True))
            self.assertEqual(dry_payload["status"], "planned")
            self.assertEqual(dry_payload["run_count"], 1)
            self.assertEqual(dry_payload["detail_count"], 1)
            run_index_record_path = paths.value("read_models_dir").rstrip("/") + "/run_index.jsonl"
            self.assertIn(run_index_record_path, dry_payload["planned_files"])
            self.assertFalse((paths.read_models_dir / "run_index.jsonl").exists())
            self.assertFalse((paths.read_models_dir / "run_details_manifest.json").exists())

            migrated = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "migrate",
                    "--project",
                    str(project),
                    "--workflow",
                    initialized.workflow_id,
                    "--split-run-summaries",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(migrated.returncode, 0, migrated.stderr + migrated.stdout)
            payload = json.loads(migrated.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "split")
            self.assertEqual(payload["run_count"], 1)
            self.assertEqual(payload["detail_count"], 1)
            self.assertTrue(legacy_path.is_file())
            run_index = read_jsonl(paths.read_models_dir / "run_index.jsonl")
            self.assertEqual(len(run_index), 1)
            self.assertEqual(run_index[0]["run_id"], "run_legacy")
            self.assertEqual(run_index[0]["detail_status"], "available")
            self.assertNotIn("details", run_index[0])
            self.assertIn("detail_path", run_index[0])
            manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["run_count"], 1)
            detail_path = project / manifest["runs"][0]["path"]
            self.assertTrue(detail_path.is_file())
            detail = json.loads(detail_path.read_text(encoding="utf-8"))
            self.assertEqual(detail["run_id"], "run_legacy")
            self.assertEqual(detail["detail_status"], "available")
            self.assertEqual(detail["details"]["available_sections"], ["agent_status"])
            build_manifest = json.loads((paths.read_models_dir / "build_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(build_manifest["run_details"][0]["run_id"], "run_legacy")
            self.assertTrue(build_manifest["run_details"][0]["detail_source"]["fingerprint"].startswith("sha256:"))
            schema_validation = validate_project_schemas(project)
            self.assertTrue(schema_validation["ok"], json.dumps(schema_validation, indent=2, sort_keys=True))
            self.assertIn(run_index_record_path, schema_validation["checked_files"])
            self.assertIn(manifest["runs"][0]["path"], schema_validation["checked_files"])

            current = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "migrate",
                    "--project",
                    str(project),
                    "--workflow",
                    initialized.workflow_id,
                    "--split-run-summaries",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(current.returncode, 0, current.stderr + current.stdout)
            current_payload = json.loads(current.stdout)
            self.assertTrue(current_payload["ok"], json.dumps(current_payload, indent=2, sort_keys=True))
            self.assertEqual(current_payload["status"], "current")

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
