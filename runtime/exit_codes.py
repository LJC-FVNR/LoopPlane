from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


EXIT_SUCCESS = 0
EXIT_GENERIC_FAILURE = 1
EXIT_INVALID_CONFIG = 2
EXIT_PLAN_MALFORMED = 3
EXIT_VALIDATION_FAILED = 4
EXIT_FAILURE_BUDGET_EXHAUSTED = 5
EXIT_WAITING_APPROVAL = 6
EXIT_WAITING_BACKGROUND_JOB = 7
EXIT_RUNNER_UNAVAILABLE = 8
EXIT_MIGRATION_REQUIRED = 9
EXIT_SECURITY_POLICY_VIOLATION = 10
EXIT_DUPLICATE_SCHEDULER = 11
EXIT_FINAL_VERIFICATION_FAILED = 12
EXIT_VERSION_CONTROL_UNAVAILABLE = 13
EXIT_HEALTH_FAILURE = 14
EXIT_NEEDS_HUMAN = 15

ADAPTER_TIMEOUT_EXIT_CODE = 124
ADAPTER_POLICY_BLOCKED_EXIT_CODE = 126
ADAPTER_COMMAND_UNAVAILABLE_EXIT_CODE = 127


def strings_from_result(result: Mapping[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        value = result.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            values.extend(strings_from_result(value, *value.keys()))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                if isinstance(item, str):
                    values.append(item)
                elif isinstance(item, Mapping):
                    values.extend(strings_from_result(item, *item.keys()))
    return values


def has_text(result: Mapping[str, Any], needles: Sequence[str], *keys: str) -> bool:
    haystack = "\n".join(strings_from_result(result, *(keys or tuple(result.keys())))).lower()
    return any(needle.lower() in haystack for needle in needles)
