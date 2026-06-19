from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from runtime.exit_codes import EXIT_GENERIC_FAILURE, EXIT_SUCCESS
from runtime.health import health_exit_code, run_health_probe


def run_watchdog_check(project: Path | str) -> dict[str, Any]:
    health = run_health_probe(Path(project))
    ok = health_exit_code(health) == EXIT_SUCCESS
    return {
        "schema_version": "1.5",
        "status": "pass" if ok else "fail",
        "health": health,
    }


def watchdog_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("status") == "pass" else EXIT_GENERIC_FAILURE
