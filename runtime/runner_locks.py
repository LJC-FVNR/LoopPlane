from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.loopplane_home import loopplane_home_layout


RUNNER_LOCK_SCHEMA_VERSION = "1.6"
RUNNER_LOCK_POLL_SECONDS = 0.05
RUNNER_LOCK_ACTIVE = "active"
RUNNER_LOCK_ABSENT = "absent"
RUNNER_LOCK_MALFORMED = "malformed"
RUNNER_LOCK_STALE = "stale"
RUNNER_LOCK_UNKNOWN = "unknown_liveness"


class RunnerResourceLockError(RuntimeError):
    pass


@dataclass
class RunnerResourceLock:
    adapter_input: Any
    lock_path: Path | None = None
    metadata: dict[str, Any] | None = None
    fd: int | None = None
    acquired: bool = False

    def __enter__(self) -> "RunnerResourceLock":
        policy = _machine_resource_policy(self.adapter_input)
        if policy is None:
            return self

        lock_key = _lock_key(policy)
        layout = loopplane_home_layout()
        layout.runner_locks_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = layout.runner_locks_dir / f"{lock_key}.lock"
        self.metadata = _lock_metadata(self.adapter_input, policy, self.lock_path)
        queue_when_busy = bool(policy.get("queue_when_busy"))
        timeout_seconds = _positive_int(getattr(self.adapter_input, "timeout_seconds", None), default=1)
        deadline = time.monotonic() + timeout_seconds

        while True:
            try:
                self.fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.acquired = True
                encoded = (json.dumps(self.metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")
                os.write(self.fd, encoded)
                os.fsync(self.fd)
                return self
            except FileExistsError as error:
                if not queue_when_busy:
                    raise RunnerResourceLockError(f"runner resource lock is already held: {self.lock_path}") from error
                if time.monotonic() >= deadline:
                    raise RunnerResourceLockError(
                        f"timed out waiting for runner resource lock: {self.lock_path}"
                    ) from error
                time.sleep(RUNNER_LOCK_POLL_SECONDS)
            except BaseException:
                self.release()
                raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def release(self) -> None:
        if not self.acquired:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            return
        self.acquired = False
        try:
            if self.lock_path is not None:
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def adapter_metadata(self) -> dict[str, Any]:
        if not self.acquired or self.lock_path is None or self.metadata is None:
            return {}
        return {
            "runner_resource_lock": {
                "acquired": True,
                "lock_path": self.lock_path.as_posix(),
                "lock_key": self.metadata.get("lock_key"),
                "lock_scope": self.metadata.get("lock_scope"),
                "global_concurrency_limit": self.metadata.get("global_concurrency_limit"),
                "queue_when_busy": self.metadata.get("queue_when_busy"),
            }
        }


def acquire_runner_resource_lock(adapter_input: Any) -> RunnerResourceLock:
    return RunnerResourceLock(adapter_input)


def machine_resource_policy_from_runner(runner_config: Any) -> Mapping[str, Any] | None:
    if isinstance(runner_config, Mapping):
        policy = runner_config.get("resource_policy")
    else:
        policy = getattr(runner_config, "resource_policy", None)
    if not isinstance(policy, Mapping):
        return None
    if policy.get("lock_scope") != "machine":
        return None
    return policy


def inspect_runner_lock(
    lock_key: str,
    *,
    runner_ids: Sequence[str] = (),
    now: datetime | None = None,
) -> dict[str, Any]:
    key = _lock_key({"lock_key": lock_key})
    layout = loopplane_home_layout()
    path = layout.runner_locks_dir / f"{key}.lock"
    base = {
        "lock_key": key,
        "path": path.as_posix(),
        "runner_ids": sorted({str(runner_id) for runner_id in runner_ids if str(runner_id).strip()}),
    }
    if not path.exists():
        return {
            **base,
            "state": RUNNER_LOCK_ABSENT,
            "ok": True,
            "message": f"No machine runner lock is currently held for {key}.",
            "guidance": "",
        }

    metadata, error = _read_lock_metadata(path)
    if error is not None:
        return _malformed_lock(base, [error])

    problems = _metadata_problems(metadata, expected_key=key, expected_path=path)
    if problems:
        return _malformed_lock(base, problems, metadata=metadata)

    assert metadata is not None
    pid = _positive_int(metadata.get("pid"), default=-1)
    heartbeat = _parse_timestamp(metadata.get("heartbeat_at"))
    acquired = _parse_timestamp(metadata.get("acquired_at"))
    observed_at = now or datetime.now(UTC)
    heartbeat_age = _age_seconds(observed_at, heartbeat) if heartbeat is not None else None
    acquired_age = _age_seconds(observed_at, acquired) if acquired is not None else None
    pid_alive = _pid_exists(pid)
    summary = _metadata_summary(metadata, pid_alive=pid_alive, heartbeat_age=heartbeat_age, acquired_age=acquired_age)

    if pid_alive is True:
        return {
            **base,
            **summary,
            "state": RUNNER_LOCK_ACTIVE,
            "ok": True,
            "message": f"Machine runner lock {key} is held by a live process.",
            "guidance": "",
        }
    if pid_alive is False:
        guidance = _stale_lock_guidance(path)
        return {
            **base,
            **summary,
            "state": RUNNER_LOCK_STALE,
            "ok": False,
            "message": f"Machine runner lock {key} is stale; recorded pid {pid} is not live. {guidance}",
            "guidance": guidance,
        }

    guidance = (
        f"Verify whether pid {pid} is still using the shared runner resource. "
        f"If it is not running, remove lock file {path.as_posix()}."
    )
    return {
        **base,
        **summary,
        "state": RUNNER_LOCK_UNKNOWN,
        "ok": False,
        "message": f"Machine runner lock {key} process liveness could not be determined. {guidance}",
        "guidance": guidance,
    }


def runner_lock_doctor_check(lock_key: str, *, runner_ids: Sequence[str] = ()) -> dict[str, Any]:
    inspection = inspect_runner_lock(lock_key, runner_ids=runner_ids)
    state = str(inspection.get("state") or RUNNER_LOCK_UNKNOWN)
    status = "ok" if inspection.get("ok") else "waiting_config"
    code_by_state = {
        RUNNER_LOCK_ABSENT: "runner_resource_lock_absent",
        RUNNER_LOCK_ACTIVE: "runner_resource_lock_active",
        RUNNER_LOCK_STALE: "stale_runner_resource_lock",
        RUNNER_LOCK_MALFORMED: "malformed_runner_resource_lock",
        RUNNER_LOCK_UNKNOWN: "runner_resource_lock_liveness_unknown",
    }
    return {
        "name": "runner_resource_lock",
        "status": status,
        "code": code_by_state.get(state, "runner_resource_lock_unknown"),
        "message": str(inspection.get("message") or ""),
        "lock_key": inspection.get("lock_key"),
        "path": inspection.get("path"),
        "state": state,
        "guidance": inspection.get("guidance") or "",
        "details": inspection,
    }


def with_runner_resource_lock_metadata(
    adapter_metadata: Mapping[str, Any],
    runner_lock: RunnerResourceLock,
) -> dict[str, Any]:
    merged = dict(adapter_metadata)
    merged.update(runner_lock.adapter_metadata())
    return merged


def _machine_resource_policy(adapter_input: Any) -> Mapping[str, Any] | None:
    return machine_resource_policy_from_runner(getattr(adapter_input, "runner_config", {}))


def _lock_key(policy: Mapping[str, Any]) -> str:
    raw = str(policy.get("lock_key") or "").strip()
    if not raw or raw in {".", ".."} or "/" in raw or "\\" in raw:
        raise RunnerResourceLockError(f"invalid machine runner lock_key: {raw!r}")
    return raw


def _lock_metadata(adapter_input: Any, policy: Mapping[str, Any], lock_path: Path) -> dict[str, Any]:
    now = _utc_timestamp()
    return {
        "schema_version": RUNNER_LOCK_SCHEMA_VERSION,
        "lock_type": "runner_resource",
        "lock_scope": "machine",
        "lock_key": _lock_key(policy),
        "lock_path": lock_path.as_posix(),
        "global_concurrency_limit": _positive_int(policy.get("global_concurrency_limit"), default=1),
        "queue_when_busy": bool(policy.get("queue_when_busy")),
        "run_id": str(getattr(adapter_input, "run_id", "")),
        "workflow_id": str(getattr(adapter_input, "workflow_id", "")),
        "runner_id": str(getattr(adapter_input, "runner_id", "")),
        "role": str(getattr(adapter_input, "role", "")),
        "task_id": getattr(adapter_input, "task_id", None),
        "pid": os.getpid(),
        "acquired_at": now,
        "heartbeat_at": now,
    }


def _positive_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _utc_timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_lock_metadata(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return None, f"invalid JSON at line {error.lineno} column {error.colno}"
    except OSError as error:
        return None, str(error)
    if not isinstance(data, Mapping):
        return None, "expected JSON object"
    return dict(data), None


def _metadata_problems(metadata: Mapping[str, Any] | None, *, expected_key: str, expected_path: Path) -> list[str]:
    if metadata is None:
        return ["missing metadata"]
    problems: list[str] = []
    if metadata.get("schema_version") != RUNNER_LOCK_SCHEMA_VERSION:
        problems.append(f"schema_version must be {RUNNER_LOCK_SCHEMA_VERSION!r}")
    if metadata.get("lock_type") != "runner_resource":
        problems.append("lock_type must be 'runner_resource'")
    if metadata.get("lock_scope") != "machine":
        problems.append("lock_scope must be 'machine'")
    if metadata.get("lock_key") != expected_key:
        problems.append(f"lock_key must match filename key {expected_key!r}")
    metadata_path = metadata.get("lock_path")
    if metadata_path != expected_path.as_posix():
        problems.append("lock_path must match the LOOPPLANE_HOME lock file path")
    if _positive_int(metadata.get("global_concurrency_limit"), default=-1) <= 0:
        problems.append("global_concurrency_limit must be a positive integer")
    if not isinstance(metadata.get("queue_when_busy"), bool):
        problems.append("queue_when_busy must be boolean")
    if _positive_int(metadata.get("pid"), default=-1) <= 0:
        problems.append("pid must be a positive integer")
    if _parse_timestamp(metadata.get("acquired_at")) is None:
        problems.append("acquired_at must be a parseable timestamp")
    if _parse_timestamp(metadata.get("heartbeat_at")) is None:
        problems.append("heartbeat_at must be a parseable timestamp")
    return problems


def _metadata_summary(
    metadata: Mapping[str, Any],
    *,
    pid_alive: bool | None,
    heartbeat_age: int | None,
    acquired_age: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": metadata.get("schema_version"),
        "runner_id": metadata.get("runner_id"),
        "run_id": metadata.get("run_id"),
        "workflow_id": metadata.get("workflow_id"),
        "role": metadata.get("role"),
        "pid": metadata.get("pid"),
        "pid_alive": pid_alive,
        "acquired_at": metadata.get("acquired_at"),
        "heartbeat_at": metadata.get("heartbeat_at"),
        "acquired_age_seconds": acquired_age,
        "heartbeat_age_seconds": heartbeat_age,
        "global_concurrency_limit": metadata.get("global_concurrency_limit"),
        "queue_when_busy": metadata.get("queue_when_busy"),
    }


def _malformed_lock(
    base: Mapping[str, Any],
    problems: Sequence[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    path = Path(str(base.get("path") or ""))
    guidance = (
        f"Verify that no runner is using the shared resource, then remove malformed lock file {path.as_posix()}."
    )
    result: dict[str, Any] = {
        **dict(base),
        "state": RUNNER_LOCK_MALFORMED,
        "ok": False,
        "message": f"Malformed machine runner lock metadata. {guidance}",
        "guidance": guidance,
        "problems": [str(problem) for problem in problems],
    }
    if metadata is not None:
        result.update(_metadata_summary(metadata, pid_alive=None, heartbeat_age=None, acquired_age=None))
    return result


def _stale_lock_guidance(path: Path) -> str:
    return (
        f"Remove stale lock file {path.as_posix()} after verifying no active runner still uses the shared resource."
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _age_seconds(now: datetime, then: datetime) -> int:
    return max(0, int((now - then).total_seconds()))


def _pid_exists(pid: int) -> bool | None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True
