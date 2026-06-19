#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime.active_projections import sync_active_workflow_projections
from runtime.path_resolution import WorkflowPaths


def _load_workflow(project: Path) -> tuple[dict[str, object], Path]:
    current_path = project / ".loopplane" / "current_workflow.json"
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        workflow_id = str(current.get("current_workflow_id") or "").strip()
        if workflow_id:
            workflow_path = project / ".loopplane" / "workflows" / workflow_id / "config" / "workflow.json"
            return json.loads(workflow_path.read_text(encoding="utf-8")), workflow_path
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    return json.loads(workflow_path.read_text(encoding="utf-8")), workflow_path


def _project_path(project: Path, value: object, fallback: str) -> Path:
    relative = str(value or fallback)
    path = Path(relative)
    if path.is_absolute():
        return path
    return project / path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: write_smoke_plan.py <project>", file=sys.stderr)
        return 2
    project = Path(argv[1]).expanduser().resolve()
    workflow, _workflow_path = _load_workflow(project)
    workflow_id = workflow["workflow_id"]
    plan_file = str(workflow.get("plan_file") or "PLAN.md")
    runtime_dir = str(workflow.get("runtime_dir") or ".loopplane/runtime")
    results_dir = str(workflow.get("results_dir") or ".loopplane/results")
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- generated_from: examples/minimal_project/write_smoke_plan.py
- active: true

## Phase P0: Minimal Release Smoke

- [ ] T001: Produce minimal release smoke artifact
  - acceptance: Minimal shell worker writes result artifact.
  - acceptance: Worker report records completion.
  - evidence: {results_dir}/T001/
  - latest: {results_dir}/T001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0; report_contains: Minimal release smoke worker completed
  - max_attempts: 3
  - approval: not_required
  - deliverables: artifacts/result.txt and report.md
"""
    plan_path = _project_path(project, plan_file, "PLAN.md")
    plan_path.write_text(plan, encoding="utf-8")
    plan_sha = "sha256:" + sha256(plan_path.read_bytes()).hexdigest()
    state_path = _project_path(project, runtime_dir, ".loopplane/runtime") / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    updated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    planning = state.get("planning") if isinstance(state.get("planning"), dict) else {}
    state.update(
        {
            "schema_version": "1.5",
            "workflow_id": workflow_id,
            "status": "active",
            "active_plan": plan_file,
            "active_plan_sha256": plan_sha,
            "updated_at": updated_at,
        }
    )
    state.pop("manual_plan_change", None)
    state["planning"] = {
        **planning,
        "status": "active",
        "ready_for_activation": True,
        "activation_run_id": "examples/minimal_project/write_smoke_plan.py",
        "plan_file": plan_file,
        "active_plan_sha256": plan_sha,
        "activated_at": updated_at,
    }
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sync_active_workflow_projections(
        project,
        workflow,
        WorkflowPaths.from_config(project, workflow),
        reason="minimal_smoke_plan_example",
    )
    print(f"wrote deterministic smoke PLAN.md for {workflow_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
