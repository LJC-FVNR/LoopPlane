from __future__ import annotations


CANONICAL_WORKER_STATUSES = frozenset(
    {
        "completed",
        "completed_with_warnings",
        "satisfied",
        "validation_candidate_failed",
        "recoverable_failed",
        "blocked_external",
        "blocked_needs_human",
        "blocked_by_scope",
        "running_background",
        "failed_system",
        "failed_agent",
        "aborted",
    }
)

SUCCESS_WORKER_STATUSES = frozenset(
    {
        "completed",
        "completed_with_warnings",
        "satisfied",
        "running_background",
    }
)

WORKER_STATUS_ALIASES = {
    "complete": "completed",
    "done": "completed",
    "finished": "completed",
    "success": "completed",
    "successful": "completed",
    "succeeded": "completed",
    "passes": "satisfied",
    "passed": "satisfied",
}


def normalize_worker_status(value: object) -> str | None:
    status = _worker_status_token(value)
    if status is None:
        return None
    return WORKER_STATUS_ALIASES.get(status, status)


def is_canonical_worker_status(value: object) -> bool:
    status = _worker_status_token(value)
    return status in CANONICAL_WORKER_STATUSES


def is_success_worker_status(value: object) -> bool:
    status = normalize_worker_status(value)
    return status in SUCCESS_WORKER_STATUSES


def _worker_status_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    status = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not status:
        return None
    return status
