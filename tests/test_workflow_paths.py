from __future__ import annotations

import json
import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from runtime.active_projections import sync_active_workflow_projections
from runtime.brief import write_project_brief
from runtime.dashboard import render_static_dashboard
from runtime.final_verifier import run_final_verifier
from runtime.health import run_health_probe
from runtime.init_workflow import LAYOUT_CANONICAL_V16, _workflow_config, init_project
from runtime.inspector import answer_inspection
from runtime.path_resolution import (
    DEFAULT_WORKFLOW_PATHS,
    WORKFLOW_PATH_FIELDS,
    PathSerializationError,
    WorkflowPathError,
    WorkflowPaths,
    deserialize_project_path,
    load_workflow_config,
    resolve_current_workflow_roots,
    serialize_project_path,
)
from runtime.plan_objectives import objective_structure_fingerprint, parse_plan_objectives
from runtime.prompt_builder import build_prompt_for_prepared_run
from runtime.read_models import rebuild_read_models
from runtime.reconciliation import run_reconciler
from runtime.scheduler import prepare_run, preview_scheduler
from runtime.schema_validation import validate_project_schemas
from runtime.validation import run_validator
from runtime.workflow_lifecycle import create_workflow_record
from tests.test_human_summaries import configure_fake_summary_agent
from tests.test_inspector import configure_fake_inspector
from tests.test_validation import disable_default_validator_agent, set_runner_enabled


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


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class WorkflowPathResolutionTest(unittest.TestCase):
    def test_resolver_maps_non_default_workflow_values_to_project_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            config = {**DEFAULT_WORKFLOW_PATHS, **CUSTOM_PATHS}

            paths = WorkflowPaths.from_config(project, config)

            self.assertEqual(set(WORKFLOW_PATH_FIELDS), set(paths.values))
            for field, value in CUSTOM_PATHS.items():
                self.assertEqual(paths.value(field), value)
                self.assertEqual(paths.path(field), project.resolve() / value)
            self.assertEqual(paths.workspace_root, project.resolve() / ".loopplane")
            self.assertEqual(paths.workflow_root, project.resolve() / ".loopplane")
            self.assertEqual(paths.template_variables()["workspace_root"], ".loopplane")
            self.assertEqual(paths.template_variables()["workflow_root"], ".loopplane")
            self.assertEqual(paths.template_variables()["runtime_dir"], CUSTOM_PATHS["runtime_dir"])

    def test_resolver_expands_workspace_and_workflow_root_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workflow_id = "wf_20260611_c0ffee00"
            config = {
                "workflow_id": workflow_id,
                "workflow_root": "{workspace_root}/workflows/{workflow_id}",
                "brief_file": "{{workflow_root}}/PROJECT_BRIEF.md",
                "runtime_dir": "{workflow_root}/runtime",
            }

            paths = WorkflowPaths.from_config(project, config)

            self.assertEqual(paths.workspace_root_value, ".loopplane")
            self.assertEqual(paths.workflow_root_value, f".loopplane/workflows/{workflow_id}")
            self.assertEqual(paths.value("brief_file"), f".loopplane/workflows/{workflow_id}/PROJECT_BRIEF.md")
            self.assertEqual(paths.value("plan_file"), f".loopplane/workflows/{workflow_id}/PLAN.md")
            self.assertEqual(paths.value("shared_context_file"), f".loopplane/workflows/{workflow_id}/SHARED_CONTEXT.md")
            self.assertEqual(paths.value("runtime_dir"), f".loopplane/workflows/{workflow_id}/runtime")
            self.assertEqual(paths.value("read_models_dir"), f".loopplane/workflows/{workflow_id}/read_models")
            self.assertEqual(paths.template_variables()["workflow_id"], workflow_id)

    def test_resolver_rejects_absolute_and_non_posix_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            with self.assertRaises(WorkflowPathError):
                WorkflowPaths.from_config(project, {"brief_file": "/tmp/brief.md"})
            with self.assertRaises(WorkflowPathError):
                WorkflowPaths.from_config(project, {"brief_file": r"docs\\BRIEF.md"})
            with self.assertRaises(WorkflowPathError):
                WorkflowPaths.from_config(project, {"brief_file": "../BRIEF.md"})

    def test_project_path_serialization_round_trips_project_relative_posix_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            target = project / ".loopplane" / "workflows" / "wf_20260611_cafe0001" / "runtime" / "state.json"

            stored = serialize_project_path(project, target)
            restored = deserialize_project_path(project, stored)

            self.assertEqual(stored, ".loopplane/workflows/wf_20260611_cafe0001/runtime/state.json")
            self.assertEqual(restored, project.resolve() / stored)
            self.assertEqual(serialize_project_path(project, project), ".")
            self.assertEqual(deserialize_project_path(project, "."), project.resolve())
            self.assertFalse(stored.startswith("/"))
            self.assertNotIn("\\", stored)

    def test_project_path_serialization_normalizes_representable_windows_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            stored = serialize_project_path(project, r".loopplane\workflows\wf_20260611_cafe0002\results")
            restored = deserialize_project_path(project, r".loopplane\workflows\wf_20260611_cafe0002\results")

            self.assertEqual(stored, ".loopplane/workflows/wf_20260611_cafe0002/results")
            self.assertEqual(restored, project.resolve() / ".loopplane/workflows/wf_20260611_cafe0002/results")

    def test_project_path_serialization_rejects_outside_project_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / "project"
            outside = base / "outside" / "state.json"

            with self.assertRaises(PathSerializationError):
                serialize_project_path(project, outside)
            with self.assertRaises(PathSerializationError):
                serialize_project_path(project, "../outside/state.json")
            with self.assertRaises(PathSerializationError):
                deserialize_project_path(project, "/tmp/outside/state.json")
            with self.assertRaises(PathSerializationError):
                deserialize_project_path(project, r"C:\Users\loopplane\state.json")

    def test_init_uses_existing_workflow_json_non_default_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            workflow_file = project / ".loopplane" / "config" / "workflow.json"
            workflow_file.parent.mkdir(parents=True)
            workflow = _workflow_config(
                "wf_20260610_1234abcd",
                "2026-06-10T08:00:00Z",
                CUSTOM_PATHS,
            )
            workflow_file.write_text(json.dumps(workflow, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = init_project(project, "Resolve through workflow.json.")

            self.assertEqual(result.workflow_id, "wf_20260610_1234abcd")
            self.assertIn(".loopplane/config/workflow.json", result.preserved)

            self.assertTrue((project / CUSTOM_PATHS["brief_file"]).is_file())
            self.assertTrue((project / CUSTOM_PATHS["plan_file"]).is_file())
            self.assertTrue((project / CUSTOM_PATHS["shared_context_file"]).is_file())
            self.assertTrue((project / CUSTOM_PATHS["results_dir"] / ".gitkeep").is_file())
            self.assertTrue((project / CUSTOM_PATHS["runtime_dir"] / "state.json").is_file())
            self.assertTrue((project / CUSTOM_PATHS["runtime_dir"] / "events" / "events_000001.jsonl").is_file())
            self.assertTrue((project / CUSTOM_PATHS["read_models_dir"] / "workflow_status.json").is_file())
            self.assertTrue((project / CUSTOM_PATHS["requests_dir"] / "change_requests.jsonl").is_file())
            self.assertTrue((project / CUSTOM_PATHS["planning_dir"] / "README.md").is_file())
            self.assertTrue((project / CUSTOM_PATHS["version_control_config_file"]).is_file())

            self.assertFalse((project / "PROJECT_BRIEF.md").exists())
            self.assertFalse((project / "PLAN.md").exists())
            self.assertFalse((project / ".loopplane" / "SHARED_CONTEXT.md").exists())
            self.assertFalse((project / ".loopplane" / "runtime" / "state.json").exists())
            self.assertFalse((project / ".loopplane" / "read_models" / "workflow_status.json").exists())
            self.assertFalse((project / ".loopplane" / "requests" / "change_requests.jsonl").exists())
            self.assertFalse((project / ".loopplane" / "planning" / "README.md").exists())
            self.assertFalse((project / ".loopplane" / "config" / "version_control.json").exists())

            plan = (project / CUSTOM_PATHS["plan_file"]).read_text(encoding="utf-8")
            shared_context = (project / CUSTOM_PATHS["shared_context_file"]).read_text(encoding="utf-8")
            dashboard = json.loads((project / ".loopplane" / "config" / "dashboard.json").read_text(encoding="utf-8"))
            security = json.loads((project / ".loopplane" / "config" / "security.json").read_text(encoding="utf-8"))
            schema_version = json.loads(
                (project / ".loopplane" / "config" / "schema_version.json").read_text(encoding="utf-8")
            )
            version_control = json.loads(
                (project / CUSTOM_PATHS["version_control_config_file"]).read_text(encoding="utf-8")
            )

            for field, value in CUSTOM_PATHS.items():
                self.assertIn(f"- {field}: {value}", plan)
                self.assertIn(f"- {field}: {value}", shared_context)
            self.assertEqual(dashboard["read_models_dir"], CUSTOM_PATHS["read_models_dir"])
            self.assertEqual(security["dashboard"]["token_file"], f"{CUSTOM_PATHS['runtime_dir']}/dashboard_token")
            self.assertNotIn("version_control.json", schema_version["files"])
            self.assertIn(CUSTOM_PATHS["version_control_config_file"], schema_version["files"])
            self.assertIn(CUSTOM_PATHS["brief_file"], version_control["path_policy"]["include"])
            self.assertIn(f"{CUSTOM_PATHS['runtime_dir']}/lock/", version_control["path_policy"]["exclude"])

    def test_load_workflow_config_resolves_current_canonical_workflow_from_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Resolve current canonical workflow.")
            flat_workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            canonical_id = "wf_20260611_c0ffee00"
            canonical_root = f".loopplane/workflows/{canonical_id}"
            canonical_config = _workflow_config(
                canonical_id,
                "2026-06-11T00:00:00Z",
                {
                    "brief_file": "{{workflow_root}}/PROJECT_BRIEF.md",
                    "plan_file": "{workflow_root}/PLAN.md",
                    "shared_context_file": "{workflow_root}/SHARED_CONTEXT.md",
                    "results_dir": "{workflow_root}/results",
                    "runtime_dir": "{workflow_root}/runtime",
                    "read_models_dir": "{workflow_root}/read_models",
                    "requests_dir": "{workflow_root}/requests",
                    "planning_dir": "{workflow_root}/planning",
                    "version_control_config_file": "{workflow_root}/config/version_control.json",
                },
            )
            config_file = project / canonical_root / "config" / "workflow.json"
            config_file.parent.mkdir(parents=True)
            config_file.write_text(json.dumps(canonical_config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workflows"].append(
                {
                    "workflow_id": canonical_id,
                    "name": "current canonical workflow",
                    "status": "active",
                    "workflow_root": canonical_root,
                    "created_at": "2026-06-11T00:00:00Z",
                    "last_seen_at": "2026-06-11T00:00:00Z",
                    "read_only": False,
                    "archived": False,
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            current_path = project / ".loopplane" / "current_workflow.json"
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["current_workflow_id"] = canonical_id
            current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            loaded = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, loaded)
            resolution = resolve_current_workflow_roots(project)

            self.assertEqual(flat_workflow["workflow_id"], registry["workflows"][0]["workflow_id"])
            self.assertEqual(loaded["workflow_id"], canonical_id)
            self.assertEqual(loaded["workspace_root"], ".loopplane")
            self.assertEqual(loaded["workflow_root"], canonical_root)
            self.assertEqual(loaded["workflow_config_file"], f"{canonical_root}/config/workflow.json")
            self.assertEqual(paths.workflow_root_value, canonical_root)
            self.assertEqual(paths.value("runtime_dir"), f"{canonical_root}/runtime")
            self.assertEqual(paths.runtime_dir, project.resolve() / canonical_root / "runtime")
            self.assertEqual(resolution.workflow_config_file, config_file.resolve())

    def test_load_workflow_config_resolves_explicit_registered_workflow_without_switching_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Resolve explicit dashboard workflow.")
            current_path = project / ".loopplane" / "current_workflow.json"
            current_before = current_path.read_bytes()
            selected_id = "wf_20260611_decafbad"
            selected_root = f".loopplane/workflows/{selected_id}"
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workflows"].append(
                {
                    "workflow_id": selected_id,
                    "name": "selected dashboard workflow",
                    "status": "archived",
                    "workflow_root": selected_root,
                    "read_only": True,
                    "archived": True,
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            loaded = load_workflow_config(project, workflow_id=selected_id)
            paths = WorkflowPaths.from_config(project, loaded)

            self.assertEqual(loaded["workflow_id"], selected_id)
            self.assertEqual(paths.workflow_root_value, selected_root)
            self.assertEqual(paths.value("read_models_dir"), f"{selected_root}/read_models")
            self.assertEqual(current_path.read_bytes(), current_before)

            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["current_workflow_id"] = "wf_20260611_deadbeef"
            current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(WorkflowPathError, "current_workflow_id .* does not reference"):
                load_workflow_config(project, workflow_id=selected_id)

    def test_load_workflow_config_preserves_v15_flat_workflow_root_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Resolve flat compatibility workflow.")

            loaded = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, loaded)
            resolution = resolve_current_workflow_roots(project)

            self.assertEqual(loaded["workflow_root"], ".loopplane")
            self.assertEqual(paths.workflow_root_value, ".loopplane")
            self.assertEqual(paths.value("runtime_dir"), ".loopplane/runtime")
            self.assertEqual(paths.runtime_dir, project.resolve() / ".loopplane" / "runtime")
            self.assertEqual(resolution.workflow_root_value, ".loopplane")

    def test_init_project_materializes_canonical_v16_workflow_local_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            result = init_project(project, "Create a canonical workflow.", layout=LAYOUT_CANONICAL_V16)
            loaded = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, loaded)
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            record = registry["workflows"][0]

            self.assertEqual(result.workflow_id, paths.workflow_id)
            self.assertEqual(paths.workflow_root_value, f".loopplane/workflows/{result.workflow_id}")
            self.assertEqual(paths.value("brief_file"), f"{paths.workflow_root_value}/PROJECT_BRIEF.md")
            self.assertEqual(paths.value("plan_file"), f"{paths.workflow_root_value}/PLAN.md")
            self.assertEqual(paths.value("shared_context_file"), f"{paths.workflow_root_value}/SHARED_CONTEXT.md")
            self.assertEqual(paths.value("planning_dir"), f"{paths.workflow_root_value}/planning")
            self.assertEqual(paths.value("runtime_dir"), f"{paths.workflow_root_value}/runtime")
            self.assertEqual(paths.value("read_models_dir"), f"{paths.workflow_root_value}/read_models")
            self.assertEqual(paths.value("requests_dir"), f"{paths.workflow_root_value}/requests")
            self.assertEqual(paths.value("results_dir"), f"{paths.workflow_root_value}/results")
            self.assertEqual(paths.workflow_config_file_value, f"{paths.workflow_root_value}/config/workflow.json")

            for relative in (
                "PROJECT_BRIEF.md",
                "PLAN.md",
                "SHARED_CONTEXT.md",
                "config/workflow.json",
                "config/agent_runners.json",
                "config/dashboard.json",
                "config/security.json",
                "config/version_control.json",
                "config/schema_version.json",
                "planning/README.md",
                "runtime/state.json",
                "runtime/events/events_000001.jsonl",
                "runtime/snapshots/snapshot_000001.json",
                "read_models/workflow_status.json",
                "requests/change_requests.jsonl",
                "results/.gitkeep",
            ):
                self.assertTrue((paths.workflow_root / relative).is_file(), relative)
            self.assertFalse((project / ".loopplane" / "runtime" / "state.json").exists())
            self.assertFalse((project / ".loopplane" / "read_models" / "workflow_status.json").exists())
            self.assertEqual(record["workflow_root"], paths.workflow_root_value)
            self.assertEqual(record["workflow_config_file"], paths.workflow_config_file_value)
            self.assertEqual(record["plan_file"], paths.value("plan_file"))
            self.assertEqual(record["runtime_dir"], paths.value("runtime_dir"))

    def test_canonical_active_workflow_projections_exclude_root_plan_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create canonical projections.", layout=LAYOUT_CANONICAL_V16)
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)

            projection_pairs = (
                (paths.brief_file, project / "PROJECT_BRIEF.md"),
                (paths.shared_context_file, project / ".loopplane" / "SHARED_CONTEXT.md"),
            )
            for source, target in projection_pairs:
                self.assertTrue(target.is_file(), target)
                self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertTrue(paths.plan_file.is_file())
            self.assertFalse((project / "PLAN.md").exists())
            metadata_path = project / ".loopplane" / "projections" / "active_workflow.json"
            self.assertTrue(metadata_path.is_file())
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["workflow_id"], paths.workflow_id)
            self.assertEqual(metadata["workflow_root"], paths.workflow_root_value)

            brief_result = write_project_brief(
                project,
                "Refresh the canonical brief and root projection.",
                source="test",
                force=True,
            )
            self.assertTrue(brief_result["ok"], json.dumps(brief_result, indent=2, sort_keys=True))
            self.assertIn("Refresh the canonical brief", paths.brief_file.read_text(encoding="utf-8"))
            self.assertEqual(
                (project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"),
                paths.brief_file.read_text(encoding="utf-8"),
            )

            paths.plan_file.write_text("# Project Plan\n\nUpdated canonical plan.\n", encoding="utf-8")
            paths.shared_context_file.write_text("# Shared Context\n\nUpdated canonical context.\n", encoding="utf-8")
            sync = sync_active_workflow_projections(project, workflow_config, paths, reason="test_update")

            self.assertTrue(sync["ok"], json.dumps(sync, indent=2, sort_keys=True))
            self.assertTrue(sync["changed"], json.dumps(sync, indent=2, sort_keys=True))
            plan_projection = next(item for item in sync["projections"] if item["field"] == "plan_file")
            self.assertEqual(plan_projection["status"], "disabled")
            self.assertFalse((project / "PLAN.md").exists())
            self.assertEqual(
                (project / ".loopplane" / "SHARED_CONTEXT.md").read_text(encoding="utf-8"),
                paths.shared_context_file.read_text(encoding="utf-8"),
            )
            self.assertEqual(paths.value("plan_file"), f"{paths.workflow_root_value}/PLAN.md")

    def test_canonical_projection_sync_preserves_unmanaged_root_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            root_brief = project / "PROJECT_BRIEF.md"
            root_brief.write_text("Human-authored root brief\n", encoding="utf-8")

            init_project(project, "Canonical source brief.", layout=LAYOUT_CANONICAL_V16)
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)
            sync = sync_active_workflow_projections(project, workflow_config, paths, reason="safety_check")

            self.assertEqual(root_brief.read_text(encoding="utf-8"), "Human-authored root brief\n")
            self.assertIn("Canonical source brief.", paths.brief_file.read_text(encoding="utf-8"))
            brief_projection = next(item for item in sync["projections"] if item["field"] == "brief_file")
            self.assertEqual(brief_projection["status"], "unsafe_existing_content")
            self.assertIn("not a managed projection", brief_projection["warning"])
            plan_projection = next(item for item in sync["projections"] if item["field"] == "plan_file")
            self.assertEqual(plan_projection["status"], "disabled")
            self.assertFalse((project / "PLAN.md").exists())
            self.assertEqual(
                (project / ".loopplane" / "SHARED_CONTEXT.md").read_text(encoding="utf-8"),
                paths.shared_context_file.read_text(encoding="utf-8"),
            )

    def test_v15_flat_compatibility_keeps_root_files_canonical_without_projection_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Flat compatibility remains canonical.")
            workflow_config = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow_config)
            before = {
                "brief": (project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"),
                "plan": (project / "PLAN.md").read_text(encoding="utf-8"),
                "shared": (project / ".loopplane" / "SHARED_CONTEXT.md").read_text(encoding="utf-8"),
            }

            sync = sync_active_workflow_projections(project, workflow_config, paths, reason="flat_compatibility")

            self.assertTrue(sync["ok"], json.dumps(sync, indent=2, sort_keys=True))
            self.assertFalse(sync["enabled"])
            self.assertEqual(sync["status"], "disabled")
            self.assertEqual(paths.workflow_root_value, ".loopplane")
            self.assertEqual(paths.value("brief_file"), "PROJECT_BRIEF.md")
            self.assertEqual(paths.value("plan_file"), "PLAN.md")
            self.assertEqual(paths.value("shared_context_file"), ".loopplane/SHARED_CONTEXT.md")
            self.assertFalse((project / ".loopplane" / "projections" / "active_workflow.json").exists())
            self.assertEqual((project / "PROJECT_BRIEF.md").read_text(encoding="utf-8"), before["brief"])
            self.assertEqual((project / "PLAN.md").read_text(encoding="utf-8"), before["plan"])
            self.assertEqual((project / ".loopplane" / "SHARED_CONTEXT.md").read_text(encoding="utf-8"), before["shared"])

    def test_canonical_read_models_use_resolved_results_and_read_model_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Build canonical read models.", layout=LAYOUT_CANONICAL_V16)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))
            plan = f"""# Project Plan

## Metadata

- workflow_id: {paths.workflow_id}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Canonical Paths

- [ ] T001: Use resolved latest fallback
  - acceptance: Read models derive latest paths from results_dir.
  - evidence: {paths.value("results_dir")}/T001/
  - depends_on: []
  - risk: low
  - validation: deterministic check
  - max_attempts: 1
"""
            paths.plan_file.write_text(plan, encoding="utf-8")

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertFalse((project / ".loopplane" / "read_models" / "plan_index.json").exists())
            plan_index = json.loads((paths.read_models_dir / "plan_index.json").read_text(encoding="utf-8"))
            task = plan_index["phases"][0]["tasks"][0]
            self.assertEqual(task["latest_path"], f"{paths.value('results_dir')}/T001/latest.json")
            self.assertNotEqual(task["latest_path"], ".loopplane/results/T001/latest.json")

    def test_canonical_read_models_serialize_project_paths_as_relative_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Serialize canonical read-model paths.", layout=LAYOUT_CANONICAL_V16)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            metrics = json.loads((paths.read_models_dir / "metrics.json").read_text(encoding="utf-8"))
            version_control = json.loads((paths.read_models_dir / "version_control_status.json").read_text(encoding="utf-8"))
            snapshot_path = metrics["event_replay"]["snapshot_path"]
            config_path = version_control["configuration"]["config_path"]
            repository_root = version_control["repository"].get("root")

            self.assertEqual(snapshot_path, f"{paths.value('runtime_dir')}/snapshots/snapshot_000001.json")
            self.assertEqual(config_path, paths.value("version_control_config_file"))
            self.assertIn(repository_root, {None, "."})
            for value in (snapshot_path, config_path, repository_root):
                if value is not None:
                    self.assertFalse(value.startswith(project.resolve().as_posix()), value)
                    self.assertNotIn("\\", value)

    def test_v15_flat_read_models_serialize_project_paths_as_relative_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Serialize flat read-model paths.")
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            result = rebuild_read_models(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(paths.workflow_root_value, ".loopplane")
            metrics = json.loads((paths.read_models_dir / "metrics.json").read_text(encoding="utf-8"))
            version_control = json.loads((paths.read_models_dir / "version_control_status.json").read_text(encoding="utf-8"))

            self.assertEqual(metrics["event_replay"]["snapshot_path"], ".loopplane/runtime/snapshots/snapshot_000001.json")
            self.assertEqual(version_control["configuration"]["config_path"], ".loopplane/config/version_control.json")

    def test_canonical_inspector_change_request_refs_use_resolved_requests_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create canonical inspector request.", layout=LAYOUT_CANONICAL_V16)
            configure_fake_inspector(project)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            result = answer_inspection(project, "Add a benchmark comparison task to PLAN.md.")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "answered")
            expected_ref = f"{paths.value('requests_dir')}/change_requests.jsonl"
            self.assertIn(expected_ref, result["response"]["refs"])
            self.assertNotIn(".loopplane/requests/change_requests.jsonl", result["response"]["refs"])
            self.assertTrue((paths.requests_dir / "change_requests.jsonl").is_file())
            self.assertFalse((project / ".loopplane" / "requests" / "change_requests.jsonl").exists())

    def test_canonical_registry_partial_path_values_do_not_fall_back_to_flat_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Create canonical registry record.", layout=LAYOUT_CANONICAL_V16)
            workflow_id = "wf_20260611_deadbeef"
            workflow_root = f".loopplane/workflows/{workflow_id}"

            result = create_workflow_record(
                project,
                workflow_id=workflow_id,
                name="partial canonical path metadata",
                workflow_root=workflow_root,
                path_values={"plan_file": f"{workflow_root}/PLAN.md"},
            )

            record = result["record"]
            self.assertEqual(record["workflow_root"], workflow_root)
            self.assertEqual(record["plan_file"], f"{workflow_root}/PLAN.md")
            self.assertEqual(record["runtime_dir"], f"{workflow_root}/runtime")
            self.assertEqual(record["read_models_dir"], f"{workflow_root}/read_models")
            self.assertEqual(record["requests_dir"], f"{workflow_root}/requests")
            self.assertEqual(record["completion_marker"], f"{workflow_root}/runtime/plan_loop_complete.json")
            self.assertNotEqual(record["runtime_dir"], ".loopplane/runtime")

    def test_canonical_runtime_surfaces_use_resolved_workflow_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Exercise canonical runtime surfaces.", layout=LAYOUT_CANONICAL_V16)
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            outputs = self._exercise_runtime_surfaces(project, paths)

            root = paths.workflow_root_value
            self.assertNotEqual(root, ".loopplane")
            self.assertEqual(outputs["schema"]["status"], "pass")
            self.assertIn(f"{root}/runtime/state.json", outputs["schema"]["checked_files"])
            self.assertIn(f"{root}/read_models/workflow_status.json", outputs["schema"]["checked_files"])
            self.assertNotIn(".loopplane/runtime/state.json", outputs["schema"]["checked_files"])
            self.assertEqual(outputs["preview"]["selected"]["expected_prompt_path"], f"{root}/runtime/runs/<run_id>/prompt.md")
            self.assertTrue((paths.runtime_dir / "preview_result.json").is_file())
            self.assertFalse((project / ".loopplane" / "runtime" / "preview_result.json").exists())
            self.assertTrue(outputs["prepared"].scheduler_run_dir.is_relative_to(paths.runtime_dir))
            self.assertTrue(outputs["prepared"].role_output_dir.is_relative_to(paths.results_dir))
            self.assertNotIn("## Configured Workflow Paths", outputs["prompt"].content)
            self.assertEqual(outputs["prompt_context_manifest"]["configured_paths"]["plan_file"], f"{root}/PLAN.md")
            self.assertEqual(outputs["prompt_context_manifest"]["configured_paths"]["runtime_dir"], f"{root}/runtime")
            self.assertEqual(outputs["prompt_metadata"]["configured_paths"]["results_dir"], f"{root}/results")
            self.assertEqual(outputs["validation"]["inputs"]["plan_file"], f"{root}/PLAN.md")
            self.assertTrue(outputs["validation"]["inputs"]["worker_run_dir"].startswith(f"{root}/results/T001/runs/"))
            self.assertEqual(outputs["reconciliation"]["latest_updates"][0]["latest_path"], f"{root}/results/T001/latest.json")
            self.assertEqual(outputs["reconciliation"]["latest_updates"][0]["validation_path"], f"{root}/results/T001/runs/{outputs['prepared'].run_id}/validation.json")
            self.assertTrue((paths.runtime_dir / "read_model_rebuild_request.json").is_file())
            self.assertFalse((project / ".loopplane" / "results" / "T001" / "latest.json").exists())
            self.assertEqual(outputs["read_models"]["read_models_dir"], f"{root}/read_models")
            self.assertEqual(outputs["dashboard"]["read_models_dir"], f"{root}/read_models")
            self.assertEqual(outputs["health"]["health_report_path"], f"{root}/runtime/health_report.json")
            self.assertEqual(outputs["final"]["final_verification_report"], f"{root}/runtime/final_verification_report.json")
            self.assertEqual(outputs["final"]["completion_marker_path"], f"{root}/runtime/plan_loop_complete.json")
            self.assertTrue((paths.runtime_dir / "plan_loop_complete.json").is_file())
            self.assertFalse((project / ".loopplane" / "runtime" / "plan_loop_complete.json").exists())

    def test_runtime_surfaces_ignore_disagreeing_loopplane_home_registry_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            fake_home = root / "home"
            initialized = init_project(project, "Global discovery must not override local truth.", layout=LAYOUT_CANONICAL_V16)
            fake_registry = fake_home / "registry" / "workspaces.json"
            fake_registry.parent.mkdir(parents=True)
            fake_payload = {
                "schema_version": "1.6",
                "authority": "discovery_only",
                "generated_at": "2026-06-11T00:00:00Z",
                "workspaces": [
                    {
                        "workspace_id": "ws_conflicting_global_workspace",
                        "name": "conflicting path match",
                        "project_root": project.resolve().as_posix(),
                        "loopplane_dir": (project / ".loopplane").resolve().as_posix(),
                        "repo_root": project.resolve().as_posix(),
                        "status": "registered",
                        "last_seen_at": "2026-06-11T00:00:00Z",
                        "current_workflow_id": "wf_20260611_deadbeef",
                        "workflow_root": "/tmp/global-registry-must-not-win",
                    },
                    {
                        "workspace_id": initialized.workspace_id,
                        "name": "conflicting id match",
                        "project_root": (root / "other-project").resolve().as_posix(),
                        "loopplane_dir": (root / "other-project" / ".loopplane").resolve().as_posix(),
                        "repo_root": (root / "other-project").resolve().as_posix(),
                        "status": "registered",
                        "last_seen_at": "2026-06-11T00:00:00Z",
                        "current_workflow_id": "wf_20260611_badf00d0",
                        "workflow_root": ".loopplane/workflows/wf_20260611_badf00d0",
                    },
                ],
            }
            write_json(fake_registry, fake_payload)
            fake_registry_before = fake_registry.read_bytes()
            original_home = os.environ.get("LOOPPLANE_HOME")
            os.environ["LOOPPLANE_HOME"] = fake_home.as_posix()
            try:
                loaded = load_workflow_config(project)
                paths = WorkflowPaths.from_config(project, loaded)
                resolution = resolve_current_workflow_roots(project)
                outputs = self._exercise_runtime_surfaces(project, paths)
            finally:
                if original_home is None:
                    os.environ.pop("LOOPPLANE_HOME", None)
                else:
                    os.environ["LOOPPLANE_HOME"] = original_home

            local_root = f".loopplane/workflows/{initialized.workflow_id}"
            self.assertEqual(loaded["workflow_id"], initialized.workflow_id)
            self.assertEqual(loaded["workflow_root"], local_root)
            self.assertEqual(paths.workflow_root_value, local_root)
            self.assertEqual(resolution.workflow_id, initialized.workflow_id)
            self.assertEqual(resolution.workflow_root_value, local_root)
            self.assertEqual(outputs["preview"]["selected"]["expected_prompt_path"], f"{local_root}/runtime/runs/<run_id>/prompt.md")
            self.assertTrue(outputs["prepared"].scheduler_run_dir.is_relative_to(paths.runtime_dir))
            self.assertEqual(outputs["validation"]["inputs"]["plan_file"], f"{local_root}/PLAN.md")
            self.assertEqual(outputs["read_models"]["read_models_dir"], f"{local_root}/read_models")
            self.assertEqual(outputs["dashboard"]["workflow_id"], initialized.workflow_id)
            self.assertEqual(outputs["dashboard"]["read_models_dir"], f"{local_root}/read_models")
            self.assertEqual(outputs["health"]["workflow_id"], initialized.workflow_id)
            self.assertEqual(outputs["final"]["workflow_id"], initialized.workflow_id)
            self.assertEqual(fake_registry.read_bytes(), fake_registry_before)

    def test_v15_flat_runtime_surfaces_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Exercise flat compatibility runtime surfaces.")
            paths = WorkflowPaths.from_config(project, load_workflow_config(project))

            outputs = self._exercise_runtime_surfaces(project, paths)

            self.assertEqual(paths.workflow_root_value, ".loopplane")
            self.assertEqual(outputs["schema"]["status"], "pass")
            self.assertIn(".loopplane/runtime/state.json", outputs["schema"]["checked_files"])
            self.assertEqual(outputs["preview"]["selected"]["expected_prompt_path"], ".loopplane/runtime/runs/<run_id>/prompt.md")
            self.assertTrue(outputs["prepared"].scheduler_run_dir.is_relative_to(paths.runtime_dir))
            self.assertTrue(outputs["prepared"].role_output_dir.is_relative_to(paths.results_dir))
            self.assertNotIn("## Configured Workflow Paths", outputs["prompt"].content)
            self.assertEqual(outputs["prompt_context_manifest"]["configured_paths"]["plan_file"], "PLAN.md")
            self.assertEqual(outputs["prompt_metadata"]["configured_paths"]["runtime_dir"], ".loopplane/runtime")
            self.assertEqual(outputs["validation"]["inputs"]["plan_file"], "PLAN.md")
            self.assertTrue(outputs["validation"]["inputs"]["worker_run_dir"].startswith(".loopplane/results/T001/runs/"))
            self.assertEqual(outputs["reconciliation"]["latest_updates"][0]["latest_path"], ".loopplane/results/T001/latest.json")
            self.assertEqual(outputs["read_models"]["read_models_dir"], ".loopplane/read_models")
            self.assertEqual(outputs["dashboard"]["read_models_dir"], ".loopplane/read_models")
            self.assertEqual(outputs["health"]["health_report_path"], ".loopplane/runtime/health_report.json")
            self.assertEqual(outputs["final"]["final_verification_report"], ".loopplane/runtime/final_verification_report.json")
            self.assertEqual(outputs["final"]["completion_marker_path"], ".loopplane/runtime/plan_loop_complete.json")

    def _exercise_runtime_surfaces(self, project: Path, paths: WorkflowPaths) -> dict[str, object]:
        configure_fake_summary_agent(project)
        disable_default_validator_agent(project)
        set_runner_enabled(project, "final_reviewer", False)
        self._write_resolved_surface_plan(paths)
        self._accept_current_plan(paths)

        schema = validate_project_schemas(project)
        preview = preview_scheduler(project, write=True)
        prepared = prepare_run(project, role="worker", task_id="T001", runner_id="worker", scheduler_owner="test")
        prompt = build_prompt_for_prepared_run(project, prepared)
        prompt_metadata = json.loads((prepared.scheduler_run_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
        prompt_context_manifest = json.loads(
            (prepared.scheduler_run_dir / "prompt_context_manifest.json").read_text(encoding="utf-8")
        )
        self._write_resolved_worker_evidence(prepared)
        validation = run_validator(project, task_id="T001", run_dir=prepared.task_evidence_run_dir)
        self.assertEqual(validation["status"], "pass", json.dumps(validation, indent=2, sort_keys=True))
        self._complete_prepared_lease(prepared)
        reconciliation = run_reconciler(project, task_id="T001", run_dir=prepared.task_evidence_run_dir)
        self.assertTrue(reconciliation["ok"], json.dumps(reconciliation, indent=2, sort_keys=True))
        self._write_final_objective_report(paths)
        read_models = rebuild_read_models(project)
        self.assertTrue(read_models["ok"], json.dumps(read_models, indent=2, sort_keys=True))
        dashboard = render_static_dashboard(project, output_dir="dashboard_static", rebuild_read_models_first=True)
        self.assertTrue(dashboard["ok"], json.dumps(dashboard, indent=2, sort_keys=True))
        health = run_health_probe(project, write=True)
        self.assertIn(health["status"], {"healthy", "healthy_with_warnings"}, json.dumps(health, indent=2, sort_keys=True))
        final = run_final_verifier(project, owner="test")
        self.assertEqual(final["status"], "pass", json.dumps(final, indent=2, sort_keys=True))
        return {
            "schema": schema,
            "preview": preview,
            "prepared": prepared,
            "prompt": prompt,
            "prompt_metadata": prompt_metadata,
            "prompt_context_manifest": prompt_context_manifest,
            "validation": validation,
            "reconciliation": reconciliation,
            "read_models": read_models,
            "dashboard": dashboard,
            "health": health,
            "final": final,
        }

    def _write_resolved_surface_plan(self, paths: WorkflowPaths) -> None:
        plan = f"""# Project Plan

## Metadata

- workflow_id: {paths.workflow_id}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Resolved Runtime Paths

- [ ] T001: Exercise resolved runtime paths
  - acceptance: Runtime surfaces use the active workflow root.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt

## Final Objective Checklist

- [ ] `FO1` Runtime surfaces are coherent across schema validation, prompt generation, validation, reconciliation, dashboard rendering, health, and final verification.
  - evidence_scope: {paths.value("runtime_dir")}; {paths.value("results_dir")}; {paths.value("read_models_dir")}
  - judgment_guidance: Confirm every exercised runtime surface resolved paths through the active workflow configuration.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - max_expansions: 2
"""
        paths.plan_file.write_text(plan, encoding="utf-8")

    def _write_final_objective_report(self, paths: WorkflowPaths) -> None:
        workflow = json.loads(paths.workflow_config_file.read_text(encoding="utf-8"))
        plan_text = paths.plan_file.read_text(encoding="utf-8")
        objectives, _errors = parse_plan_objectives(plan_text)
        workflow_objectives = [objective for objective in objectives if objective.scope == "workflow"]
        report_path = paths.runtime_dir / "objectives" / "final_objective_verification.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            report_path,
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
                        "evidence_reviewed": [
                            paths.value("runtime_dir"),
                            paths.value("results_dir"),
                            paths.value("read_models_dir"),
                        ],
                        "agent_rationale": "Runtime path fixture exercised all target surfaces successfully.",
                        "expandable": False,
                    }
                ],
                "summary": {"total": 1, "passed": 1, "unmet": 0, "blocked": 0, "waived": 0},
            },
        )

    def _accept_current_plan(self, paths: WorkflowPaths) -> None:
        state_path = paths.runtime_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["active_plan_sha256"] = "sha256:" + sha256(paths.plan_file.read_bytes()).hexdigest()
        state["configuration_problems"] = [
            problem
            for problem in state.get("configuration_problems", [])
            if isinstance(problem, dict) and problem.get("code") != "manual_plan_change_detected"
        ]
        state.pop("manual_plan_change", None)
        write_json(state_path, state)

    def _write_resolved_worker_evidence(self, prepared: object) -> None:
        run_dir = prepared.task_evidence_run_dir
        assert isinstance(run_dir, Path)
        (run_dir / "artifacts" / "result.txt").write_text("result\n", encoding="utf-8")
        (run_dir / "report.md").write_text("# Worker Report\n\nResolved runtime path evidence.\n", encoding="utf-8")
        (run_dir / "commands.sh").write_text("python build_result.py\n", encoding="utf-8")
        (run_dir / "logs" / "stdout.log").write_text("ok\n", encoding="utf-8")
        (run_dir / "git").mkdir(exist_ok=True)
        (run_dir / "git" / "changed_files.json").write_text(
            json.dumps({"schema_version": "1.5", "changed_files": [{"path": "src/example.py", "status": "modified"}]}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        status = {
            "schema_version": "1.5",
            "run_id": prepared.run_id,
            "task_id": "T001",
            "primary_task_id": "T001",
            "phase": "Phase P0: Resolved Runtime Paths",
            "status": "completed",
            "next_prompt_ready": True,
            "project_changes": [],
            "commands_run": [{"cmd": "python build_result.py", "exit_code": 0}],
            "key_outputs": ["artifacts/result.txt"],
            "evidence_satisfies": [
                {
                    "task_id": "T001",
                    "relationship": "primary",
                    "acceptance_claimed": ["Runtime surfaces use the active workflow root."],
                    "evidence": ["artifacts/result.txt"],
                }
            ],
            "validation_claim": {
                "claim": "completed",
                "checks_claimed": [{"name": "file_exists", "status": "pass"}],
                "limitations": [],
            },
            "summary_candidate": {"one_line": "Resolved path evidence recorded.", "highlights": [], "warnings": [], "blockers": []},
            "background": {"pids": [], "commands": [], "logs": [], "heartbeat_required": False, "wake_next_agent_when": None},
            "repair_attempts": [],
            "known_risks": [],
            "remaining_incomplete_items": [],
        }
        write_json(run_dir / "agent_status.json", status)

    def _complete_prepared_lease(self, prepared: object) -> None:
        lease = json.loads(prepared.active_run_lease_path.read_text(encoding="utf-8"))
        lease["status"] = "completed"
        write_json(prepared.active_run_lease_path, lease)


if __name__ == "__main__":
    unittest.main()
