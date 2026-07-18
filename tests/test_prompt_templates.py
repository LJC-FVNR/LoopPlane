from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"


class PromptTemplateGitBoundaryTest(unittest.TestCase):
    def test_worker_and_recovery_prompts_include_unattended_full_access_policy(self) -> None:
        for name in ("worker_prompt.template.md", "recovery_prompt.template.md"):
            with self.subTest(template=name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")

                self.assertIn("unattended", text)
                self.assertIn("full-access mode", text)
                self.assertIn("use `loopplane vc`", text)
                self.assertIn("complete", text)

    def test_worker_and_recovery_prompts_keep_report_separate_from_human_summary(self) -> None:
        for name in ("worker_prompt.template.md", "recovery_prompt.template.md"):
            with self.subTest(template=name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")

                self.assertIn("handoff evidence", text)
                self.assertIn("future agents", text)
                self.assertIn("not as the leadership-facing human summary", text)
                self.assertIn("human summary is generated separately", text)

    def test_worker_and_recovery_prompts_adopt_live_background_work(self) -> None:
        for name in ("worker_prompt.template.md", "recovery_prompt.template.md"):
            with self.subTest(template=name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")
                normalized = " ".join(text.split())

                self.assertIn("background registry", text)
                self.assertIn("adopt and monitor", text)
                self.assertIn("running_background", text)
                self.assertIn("never launch a duplicate", normalized)

    def test_shared_context_template_includes_worker_git_boundaries(self) -> None:
        text = (TEMPLATES / "SHARED_CONTEXT.template.md").read_text(encoding="utf-8")

        self.assertIn("## Worker Git Boundaries", text)
        self.assertIn("unattended full-access mode", text)
        self.assertIn("local Git commands and `loopplane vc` commands", text)
        self.assertIn("workspace boundary", text)

    def test_plan_and_planner_templates_include_objective_gates(self) -> None:
        plan_text = (TEMPLATES / "PLAN.template.md").read_text(encoding="utf-8")
        planner_text = (TEMPLATES / "planner_prompt.template.md").read_text(encoding="utf-8")

        for text in (plan_text, planner_text):
            with self.subTest(template="plan" if text is plan_text else "planner"):
                self.assertIn("### Phase Objective Checklist", text)
                self.assertIn("## Final Objective Checklist", text)
                self.assertIn("verifier: objective_verifier", text)
                self.assertIn("unmet_action: self_expand", text)
                self.assertIn("max_expansions", text)

    def test_summary_prompt_is_leadership_facing_without_markdown_template(self) -> None:
        text = (TEMPLATES / "summary_prompt.template.md").read_text(encoding="utf-8")

        self.assertIn("leadership-facing progress report", text)
        self.assertIn("Do not use a fixed outline", text)
        self.assertIn("It has no required", text)
        self.assertIn("headings or sections", text)
        self.assertIn("Do not enumerate or cite", text)
        self.assertNotIn("Executive Brief", text)
        self.assertNotIn("Progress Narrative", text)
        self.assertNotIn("Traceability", text)


if __name__ == "__main__":
    unittest.main()
