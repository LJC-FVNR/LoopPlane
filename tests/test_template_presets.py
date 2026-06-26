from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.init_workflow import LAYOUT_CANONICAL_V16, init_project
from runtime.planning import activate_plan
from runtime.schema_validation import validate_project_schemas
from runtime.template_presets import (
    TemplatePresetError,
    create_workflow_from_template,
    doctor_workflow_template,
    list_workflow_templates,
    load_workflow_template,
    merge_template_inputs,
    render_template_preview,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_MINIMAL_PRESET = (
    REPO_ROOT / "templates" / "workflows" / "research-topic-exploration" / "examples" / "minimal.preset.json"
)


class WorkflowTemplatePresetTest(unittest.TestCase):
    def test_builtin_templates_are_discoverable_doctorable_and_renderable(self) -> None:
        listed = list_workflow_templates()

        self.assertTrue(listed["ok"], json.dumps(listed, indent=2, sort_keys=True))
        template_ids = {template["id"] for template in listed["templates"]}
        self.assertIn("research-topic-exploration", template_ids)
        self.assertIn("dashboard-performance-investigation", template_ids)

        doctor = doctor_workflow_template("research-topic-exploration")
        self.assertTrue(doctor["ok"], json.dumps(doctor, indent=2, sort_keys=True))

        template = load_workflow_template("research-topic-exploration")
        inputs = merge_template_inputs(
            template,
            overrides={
                "topic": "Adaptive Sparse Routing",
                "enable_ablation": False,
            },
        )
        self.assertEqual(inputs["topic_slug"], "adaptive_sparse_routing")
        self.assertEqual(inputs["final_report_path"], "reports/adaptive_sparse_routing_final.md")

        rendered = render_template_preview(
            template_id="research-topic-exploration",
            overrides={
                "topic": "Adaptive Sparse Routing",
                "enable_ablation": False,
            },
        )

        self.assertTrue(rendered["ok"], json.dumps(rendered, indent=2, sort_keys=True))
        plan_text = rendered["rendered"]["plan_draft"]["text"]
        self.assertIn("P1.T002: Perform focused literature", plan_text)
        self.assertNotIn("Phase P4: Ablation And Robustness", plan_text)
        self.assertIn("reports/adaptive_sparse_routing_final.md", plan_text)

    def test_template_input_validation_rejects_unsafe_project_paths(self) -> None:
        template = load_workflow_template("research-topic-exploration")

        with self.assertRaises(TemplatePresetError) as raised:
            merge_template_inputs(
                template,
                overrides={
                    "topic": "Unsafe path fixture",
                    "final_report_path": "../outside.md",
                },
            )

        self.assertEqual(raised.exception.status, "input_validation_failed")
        self.assertIn("final_report_path: path must not contain parent traversal", raised.exception.errors)

        rendered = render_template_preview(
            template_id="research-topic-exploration",
            overrides={
                "topic": "Invalid render fixture",
                "unknown_input": "nope",
            },
        )
        self.assertFalse(rendered["ok"])
        self.assertEqual(rendered["status"], "input_validation_failed")
        self.assertIn("unknown template input(s): unknown_input", rendered["errors"])

    def test_create_from_preset_writes_portable_instance_readiness_and_activates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            initialized = init_project(project, "Initial workflow for template fixture.", layout=LAYOUT_CANONICAL_V16)

            created = create_workflow_from_template(project, preset_path=RESEARCH_MINIMAL_PRESET)

            self.assertTrue(created["ok"], json.dumps(created, indent=2, sort_keys=True))
            self.assertEqual(created["status"], "created_from_template")
            self.assertNotEqual(created["workflow_id"], initialized.workflow_id)
            self.assertEqual(created["current_workflow_id"], created["workflow_id"])
            self.assertEqual(created["readiness_report"]["status"], "ready_for_activation")
            self.assertEqual(created["readiness_report"]["draft_source"], "template")

            workflow_root = project / created["workflow_root"]
            instance_path = workflow_root / "template_instance.json"
            readiness_path = workflow_root / "planning" / "plan_readiness_report.json"
            draft_path = workflow_root / "planning" / "PLAN_DRAFT.md"
            inactive_plan_path = workflow_root / "PLAN.md"
            self.assertTrue(instance_path.is_file())
            self.assertTrue(readiness_path.is_file())
            self.assertTrue(draft_path.is_file())
            self.assertIn("- active: false", inactive_plan_path.read_text(encoding="utf-8"))

            instance = json.loads(instance_path.read_text(encoding="utf-8"))
            self.assertEqual(
                instance["preset"]["path"],
                "templates/workflows/research-topic-exploration/examples/minimal.preset.json",
            )
            self.assertNotIn(REPO_ROOT.as_posix(), json.dumps(instance))
            self.assertTrue(instance["activation"]["requires_activate_plan"])

            schema_result = validate_project_schemas(project)
            self.assertTrue(schema_result["ok"], json.dumps(schema_result, indent=2, sort_keys=True))
            self.assertIn(
                f".loopplane/workflows/{created['workflow_id']}/template_instance.json",
                schema_result["checked_files"],
            )

            activated = activate_plan(project)

            self.assertTrue(activated["ok"], json.dumps(activated, indent=2, sort_keys=True))
            self.assertEqual(activated["status"], "activated")
            self.assertIn("- active: true", inactive_plan_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
