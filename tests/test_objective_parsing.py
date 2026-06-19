from __future__ import annotations

import unittest

from runtime.final_verifier import _parse_plan_tasks as parse_final_tasks
from runtime.plan_objectives import parse_plan_objectives
from runtime.planning import _parse_task_blocks
from runtime.reconciliation import parse_plan_tasks as parse_reconciliation_tasks
from runtime.validation import parse_plan_tasks as parse_validation_tasks


PLAN_WITH_OBJECTIVES = """# Project Plan

## Metadata

- active: true

## Phase P1: Demo

- [ ] T1: Build thing
  - risk: medium
  - validation: file_exists: report.md
  - depends_on: []

### Phase Objective Checklist

- [ ] `PO1` Thing is useful.
  - evidence_scope: report
  - judgment_guidance: Judge usefulness.
  - verifier: objective_verifier
  - unmet_action: self_expand
  - risk: low

## Final Objective Checklist

- [ ] `WO1` Workflow is reviewable.
  - evidence_scope: all deliverables
  - verifier: objective_verifier
  - unmet_action: self_expand
"""


class ObjectiveParsingTest(unittest.TestCase):
    def test_objective_lines_parse_as_objectives_not_tasks(self) -> None:
        objectives, errors = parse_plan_objectives(PLAN_WITH_OBJECTIVES)

        self.assertEqual(errors, [])
        self.assertEqual([objective.objective_id for objective in objectives], ["PO1", "WO1"])
        self.assertEqual(objectives[0].scope, "phase")
        self.assertEqual(objectives[0].phase_id, "P1")
        self.assertEqual(objectives[1].scope, "workflow")

        validation_tasks = parse_validation_tasks(PLAN_WITH_OBJECTIVES)
        reconciliation_tasks = parse_reconciliation_tasks(PLAN_WITH_OBJECTIVES)
        planning_tasks = _parse_task_blocks(PLAN_WITH_OBJECTIVES)
        final_tasks, final_errors = parse_final_tasks(PLAN_WITH_OBJECTIVES)

        self.assertEqual(set(validation_tasks), {"T1"})
        self.assertEqual(set(reconciliation_tasks), {"T1"})
        self.assertEqual([task["task_id"] for task in planning_tasks], ["T1"])
        self.assertEqual([task.task_id for task in final_tasks], ["T1"])
        self.assertEqual(final_errors, [])

    def test_objective_metadata_does_not_modify_previous_task_fields(self) -> None:
        validation_task = parse_validation_tasks(PLAN_WITH_OBJECTIVES)["T1"]
        reconciliation_task = parse_reconciliation_tasks(PLAN_WITH_OBJECTIVES)["T1"]
        planning_task = _parse_task_blocks(PLAN_WITH_OBJECTIVES)[0]
        final_task = parse_final_tasks(PLAN_WITH_OBJECTIVES)[0][0]

        self.assertEqual(validation_task.risk, "medium")
        self.assertEqual(reconciliation_task.fields["risk"], ("medium",))
        self.assertEqual(planning_task["fields"]["risk"], "medium")
        self.assertEqual(final_task.fields["risk"], ("medium",))
        for leaked in ("evidence_scope", "judgment_guidance", "verifier", "unmet_action"):
            self.assertNotIn(leaked, reconciliation_task.fields)
            self.assertNotIn(leaked, planning_task["fields"])
            self.assertNotIn(leaked, final_task.fields)

    def test_duplicate_objective_ids_are_reported_across_scopes(self) -> None:
        duplicate_plan = PLAN_WITH_OBJECTIVES.replace("`WO1`", "`PO1`")

        objectives, errors = parse_plan_objectives(duplicate_plan)

        self.assertEqual([objective.objective_id for objective in objectives], ["PO1", "PO1"])
        self.assertTrue(any("Duplicate objective id 'PO1'" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
