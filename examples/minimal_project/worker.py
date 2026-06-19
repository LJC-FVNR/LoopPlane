#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    run_dir = Path(os.environ["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"])
    task_id = os.environ["LOOPPLANE_TASK_ID"]
    run_id = os.environ["LOOPPLANE_RUN_ID"]
    artifacts = run_dir / "artifacts"
    logs = run_dir / "logs"
    raw = run_dir / "raw"
    artifacts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    raw.mkdir(parents=True, exist_ok=True)

    result_path = artifacts / "result.txt"
    report_path = run_dir / "report.md"
    commands_path = run_dir / "commands.sh"
    result_path.write_text("minimal release smoke result\n", encoding="utf-8")
    report_path.write_text(
        "# Worker Report\n\nMinimal release smoke worker completed.\n",
        encoding="utf-8",
    )
    commands_path.write_text("python3 examples/minimal_project/worker.py\n", encoding="utf-8")

    status = {
        "schema_version": "1.5",
        "run_id": run_id,
        "task_id": task_id,
        "primary_task_id": task_id,
        "phase": "Phase P0: Minimal Release Smoke",
        "status": "completed",
        "next_prompt_ready": True,
        "project_changes": [],
        "commands_run": [{"cmd": "python3 examples/minimal_project/worker.py", "exit_code": 0}],
        "key_outputs": [str(result_path), str(report_path)],
        "evidence_satisfies": [
            {
                "task_id": task_id,
                "relationship": "primary",
                "acceptance_claimed": [
                    "Minimal shell worker writes result artifact.",
                    "Worker report records completion.",
                ],
                "evidence": [str(result_path), str(report_path)],
            }
        ],
        "validation_claim": {
            "claim": "completed",
            "checks_claimed": [{"name": "minimal_release_smoke", "status": "pass"}],
            "limitations": [],
        },
        "summary_candidate": {
            "one_line": "Minimal release smoke worker completed.",
            "highlights": ["result artifact written"],
            "warnings": [],
            "blockers": [],
        },
        "background": {
            "pids": [],
            "commands": [],
            "logs": [],
            "heartbeat_required": False,
            "wake_next_agent_when": None,
        },
        "repair_attempts": [],
        "known_risks": [],
        "remaining_incomplete_items": [],
    }
    (run_dir / "agent_status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("minimal release smoke worker completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
