#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from runtime.objective_verification import apply_objective_verification_report, objective_report_path
from runtime.path_resolution import WorkflowPaths
from runtime.plan_objectives import ObjectiveRecord, objective_structure_fingerprint, parse_plan_objectives


def _load_workflow(project: Path) -> dict[str, object]:
    current_path = project / ".loopplane" / "current_workflow.json"
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        workflow_id = str(current.get("current_workflow_id") or "").strip()
        if workflow_id:
            workflow_path = project / ".loopplane" / "workflows" / workflow_id / "config" / "workflow.json"
            return json.loads(workflow_path.read_text(encoding="utf-8"))
    workflow_path = project / ".loopplane" / "config" / "workflow.json"
    return json.loads(workflow_path.read_text(encoding="utf-8"))


def _objective_result(objective: ObjectiveRecord, *, evidence_scope: str) -> dict[str, object]:
    return {
        "objective_id": objective.objective_id,
        "status": "satisfied",
        "verdict": "satisfied",
        "confidence": "high",
        "evidence_reviewed": [
            f"{evidence_scope.rstrip('/')}/latest.json",
            f"{evidence_scope.rstrip('/')}/runs/",
        ],
        "agent_rationale": "Deterministic minimal smoke evidence satisfies this objective.",
        "expandable": False,
    }


def _write_report(
    project: Path,
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    scope: str,
    phase_id: str | None,
    phase_title: str | None,
    selected_objectives: Sequence[ObjectiveRecord],
) -> Path:
    plan_text = paths.plan_file.read_text(encoding="utf-8")
    plan_sha = "sha256:" + sha256(plan_text.encode("utf-8")).hexdigest()
    verified_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report_path = objective_report_path(paths, scope=scope, phase_id=phase_id)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    results = [
        _objective_result(
            objective,
            evidence_scope=(objective.fields.get("evidence_scope") or (".loopplane/results/T001/",))[0],
        )
        for objective in selected_objectives
    ]
    report = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "scope": scope,
        "phase_id": phase_id,
        "phase_title": phase_title,
        "status": "satisfied",
        "verified_at": verified_at,
        "plan_sha256": plan_sha,
        "objective_structure_fingerprint": objective_structure_fingerprint(
            plan_text,
            objectives=selected_objectives,
        ),
        "objective_results": results,
        "summary": {
            "total": len(results),
            "passed": len(results),
            "unmet": 0,
            "blocked": 0,
            "waived": 0,
        },
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    apply_result = apply_objective_verification_report(project, report_path, owner="minimal_smoke", write=True)
    if not apply_result.get("ok"):
        raise SystemExit("objective report apply failed: " + json.dumps(apply_result, indent=2, sort_keys=True))
    return report_path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: write_smoke_objective_reports.py <project>", file=sys.stderr)
        return 2
    project = Path(argv[1]).expanduser().resolve()
    workflow = _load_workflow(project)
    workflow_id = str(workflow["workflow_id"])
    paths = WorkflowPaths.from_config(project, workflow)
    plan_text = paths.plan_file.read_text(encoding="utf-8")
    objectives, parse_errors = parse_plan_objectives(plan_text)
    if parse_errors:
        print("objective parse errors:", file=sys.stderr)
        for error in parse_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    if not objectives:
        print("no objectives found in PLAN.md", file=sys.stderr)
        return 1

    written: list[Path] = []
    phase_keys: list[tuple[str | None, str | None]] = []
    for objective in objectives:
        if objective.scope == "phase":
            key = (objective.phase_id, objective.phase_title)
            if key not in phase_keys:
                phase_keys.append(key)

    for phase_id, phase_title in phase_keys:
        selected = [
            objective
            for objective in objectives
            if objective.scope == "phase" and objective.phase_id == phase_id
        ]
        written.append(
            _write_report(
                project,
                paths,
                workflow_id=workflow_id,
                scope="phase",
                phase_id=phase_id,
                phase_title=phase_title,
                selected_objectives=selected,
            )
        )

    workflow_objectives = [objective for objective in objectives if objective.scope == "workflow"]
    if workflow_objectives:
        written.append(
            _write_report(
                project,
                paths,
                workflow_id=workflow_id,
                scope="workflow",
                phase_id=None,
                phase_title=None,
                selected_objectives=workflow_objectives,
            )
        )

    for path in written:
        print(f"wrote and applied {path.relative_to(project).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
