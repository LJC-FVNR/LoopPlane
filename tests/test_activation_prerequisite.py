from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALLED_SKILL = PROJECT_ROOT / ".codex" / "skills" / "loopplane"
LOOPPLANE_ROOT = INSTALLED_SKILL if INSTALLED_SKILL.is_dir() else PROJECT_ROOT
sys.path.insert(0, str(LOOPPLANE_ROOT))

from runtime.init_workflow import init_project  # noqa: E402
import runtime.planning as planning_runtime  # noqa: E402
from runtime.planning import activate_plan, run_auditor, run_planner  # noqa: E402


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _configure_noop_runner(project: Path, runner_id: str) -> None:
    path = project / ".loopplane" / "config" / "agent_runners.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["runners"][runner_id].update({"adapter": "noop", "command": "noop", "enabled": True})
    _write_json(path, config)


def _make_fixture(project: Path, *, omit_checkpoint_path: str | None = None) -> dict[str, Path | str]:
    initialized = init_project(project, "Exercise fail-closed activation prerequisite proof.")
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow["planning"]["auditor_required"] = True
    workflow["validation"] = {
        "validator_agent_mode": "always",
        "validator_agent_for_high_risk": True,
    }
    _write_json(workflow_path, workflow)
    _configure_noop_runner(project, "planner")
    _configure_noop_runner(project, "auditor")

    plan_result = run_planner(project)
    if plan_result.get("ok") is not True:
        raise AssertionError(json.dumps(plan_result, indent=2, sort_keys=True))
    planner_run_id = str(plan_result["run_id"])
    planning_dir = project / ".loopplane" / "planning"
    draft_path = planning_dir / "PLAN_DRAFT.md"
    draft_path.write_text(
        draft_path.read_text(encoding="utf-8")
        + "\n<!-- activation gate: validator_activation_prerequisite.json -->\n",
        encoding="utf-8",
    )

    proof_paths = {
        "validation": "proof/validation.py",
        "reconciliation": "proof/reconciliation.py",
        "planning": "proof/planning.py",
        "activation_prerequisites": "proof/activation_prerequisites.py",
        "validator_test": "tests/validator_regression.py",
        "activation_test": "tests/activation_regression.py",
    }
    for label, relative in proof_paths.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# pinned {label}\n", encoding="utf-8")

    readiness_path = planning_dir / "plan_readiness_report.json"
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    readiness["generated_at"] = "2026-01-01T00:00:01Z"
    _write_json(readiness_path, readiness)

    vc_path = project / ".loopplane" / "config" / "version_control.json"
    vc = json.loads(vc_path.read_text(encoding="utf-8"))
    vc["path_policy"]["include"] = [
        ".loopplane/config/",
        ".loopplane/planning/",
        "proof/",
        "tests/",
    ]
    if omit_checkpoint_path is not None:
        vc["path_policy"]["exclude"].append(omit_checkpoint_path)
    _write_json(vc_path, vc)

    prerequisite_path = planning_dir / "runs" / planner_run_id / "validator_activation_prerequisite.json"
    audit_path = planning_dir / "audit_report.json"
    prerequisite_rel = prerequisite_path.relative_to(project).as_posix()
    static_paths = [
        workflow_path.relative_to(project).as_posix(),
        proof_paths["validation"],
        proof_paths["reconciliation"],
        proof_paths["planning"],
        proof_paths["activation_prerequisites"],
        proof_paths["validator_test"],
        proof_paths["activation_test"],
        draft_path.relative_to(project).as_posix(),
        vc_path.relative_to(project).as_posix(),
    ]
    pinned_paths = static_paths + [
        readiness_path.relative_to(project).as_posix(),
        audit_path.relative_to(project).as_posix(),
        prerequisite_rel,
    ]
    record: dict[str, object] = {
        "schema_version": "1.2",
        "workflow_id": initialized.workflow_id,
        "run_id": planner_run_id,
        "recorded_at": "2026-01-01T00:00:00Z",
        "status": "ready_for_fresh_audit",
        "current_configuration": {
            "path": workflow_path.relative_to(project).as_posix(),
            "sha256": _sha256(workflow_path),
            "validation.validator_agent_mode": "always",
            "validation.validator_agent_for_high_risk": True,
        },
        "execution_runtime": {
            key: {"path": proof_paths[key], "sha256": _sha256(project / proof_paths[key])}
            for key in ("validation", "reconciliation", "planning", "activation_prerequisites")
        },
        "integration_regression": {
            "path": proof_paths["validator_test"],
            "sha256": _sha256(project / proof_paths["validator_test"]),
            "status": "passed",
            "exit_code": 0,
        },
        "activation_preflight_regression": {
            "path": proof_paths["activation_test"],
            "sha256": _sha256(project / proof_paths["activation_test"]),
            "status": "passed",
            "exit_code": 0,
        },
        "plan_draft": {
            "path": draft_path.relative_to(project).as_posix(),
            "sha256": _sha256(draft_path),
        },
        "durability_gate": {
            "version_control_config_path": vc_path.relative_to(project).as_posix(),
            "version_control_config_sha256": _sha256(vc_path),
            "checkpoint_backend": "managed_refs",
            "required_checkpoint_reason": "before_plan_activation",
            "checkpoint_is_fail_closed": True,
            "checkpoint_must_precede_plan_write": True,
            "pinned_paths_must_be_included": pinned_paths,
        },
    }
    _write_json(prerequisite_path, record)

    audit_result = run_auditor(project)
    if audit_result.get("ok") is not True:
        raise AssertionError(json.dumps(audit_result, indent=2, sort_keys=True))
    return {
        "plan": project / "PLAN.md",
        "draft": draft_path,
        "readiness": readiness_path,
        "audit": audit_path,
        "prerequisite": prerequisite_path,
        "validation": project / proof_paths["validation"],
        "workflow_id": initialized.workflow_id,
        "planner_run_id": planner_run_id,
    }


class ActivationPrerequisiteIntegrationTests(unittest.TestCase):
    def _assert_activation_blocks_without_plan_mutation(self, project: Path, expected_code: str) -> None:
        plan = project / "PLAN.md"
        before = plan.read_bytes()
        result = activate_plan(project)
        self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
        self.assertIn(expected_code, result["blocker_codes"])
        self.assertEqual(plan.read_bytes(), before)
        self.assertIn("- active: false", plan.read_text(encoding="utf-8"))

    def test_valid_prerequisite_and_exact_checkpoint_allow_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            _make_fixture(project)

            result = activate_plan(project)

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "activated")
            self.assertIn("- active: true", (project / "PLAN.md").read_text(encoding="utf-8"))
            events = (project / ".loopplane" / "planning" / "activation_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("activation_prerequisite_preflight_passed", events)
            self.assertIn("activation_checkpoint_bytes_verified", events)

    def test_missing_malformed_stale_identity_and_hash_drift_fail_closed(self) -> None:
        cases = ("missing", "malformed", "stale", "workflow", "run", "hash_drift", "audit_binding")
        expected = {
            "missing": "activation_prerequisite_missing",
            "malformed": "activation_prerequisite_malformed",
            "stale": "activation_prerequisite_stale",
            "workflow": "activation_prerequisite_workflow_mismatch",
            "run": "activation_prerequisite_run_mismatch",
            "hash_drift": "activation_prerequisite_hash_drift",
            "audit_binding": "activation_prerequisite_audit_binding_mismatch",
        }
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp) / "project"
                paths = _make_fixture(project)
                prerequisite = Path(paths["prerequisite"])
                if case == "missing":
                    prerequisite.unlink()
                elif case == "malformed":
                    prerequisite.write_text("{not-json\n", encoding="utf-8")
                elif case in {"stale", "workflow", "run"}:
                    record = json.loads(prerequisite.read_text(encoding="utf-8"))
                    if case == "stale":
                        record["recorded_at"] = "2999-01-01T00:00:00Z"
                    elif case == "workflow":
                        record["workflow_id"] = "wf_wrong"
                    else:
                        record["run_id"] = "plan_wrong"
                    _write_json(prerequisite, record)
                elif case == "hash_drift":
                    Path(paths["validation"]).write_text("# drifted after audit\n", encoding="utf-8")
                else:
                    audit_path = Path(paths["audit"])
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                    audit["activation_bindings"]["plan_draft_sha256"] = "sha256:" + "0" * 64
                    _write_json(audit_path, audit)

                self._assert_activation_blocks_without_plan_mutation(project, expected[case])

    def test_checkpoint_omission_is_detected_before_plan_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            _make_fixture(project, omit_checkpoint_path="proof/reconciliation.py")

            self._assert_activation_blocks_without_plan_mutation(project, "activation_checkpoint_path_missing")

    def test_checkpoint_hash_race_is_detected_before_plan_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths = _make_fixture(project)
            plan = project / "PLAN.md"
            before = plan.read_bytes()
            original_checkpoint = planning_runtime.create_git_checkpoint

            def checkpoint_after_drift(*args: object, **kwargs: object) -> dict[str, object]:
                if kwargs.get("reason") == "before_plan_activation":
                    Path(paths["validation"]).write_text("# raced after preflight\n", encoding="utf-8")
                return original_checkpoint(*args, **kwargs)

            with mock.patch.object(planning_runtime, "create_git_checkpoint", side_effect=checkpoint_after_drift):
                result = planning_runtime.activate_plan(project)

            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("activation_checkpoint_hash_mismatch", result["blocker_codes"])
            self.assertEqual(plan.read_bytes(), before)
            self.assertIn("- active: false", plan.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
