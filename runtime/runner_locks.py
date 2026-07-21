from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.loopplane_home import loopplane_home_layout
from runtime.process_identity import (
    host_is_local as process_host_is_local,
    pid_exists as process_pid_exists,
    process_start_time as read_process_start_time,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - machine-scoped locks are fail-closed off POSIX.
    fcntl = None  # type: ignore[assignment]


RUNNER_LOCK_SCHEMA_VERSION = "1.6"
RUNNER_LOCK_POLL_SECONDS = 1.0
RUNNER_LOCK_HEARTBEAT_INTERVAL_SECONDS = 30.0
RUNNER_LOCK_LEASE_TTL_SECONDS = 120
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
    heartbeat_stop: threading.Event | None = None
    heartbeat_thread: threading.Thread | None = None

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
                self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                self.acquired = True
                if not _acquire_runner_file_lock(self.fd, blocking=True):
                    raise RunnerResourceLockError(
                        f"unable to acquire newly created runner resource lock: {self.lock_path}"
                    )
                _write_runner_lock_fd(self.fd, self.metadata)
                self._start_heartbeat()
                return self
            except FileExistsError as error:
                if _reclaim_stale_runner_lock(self.lock_path, self.adapter_input):
                    continue
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
        if self.heartbeat_stop is not None:
            self.heartbeat_stop.set()
        if self.heartbeat_thread is not None:
            self.heartbeat_thread.join(timeout=max(1.0, RUNNER_LOCK_HEARTBEAT_INTERVAL_SECONDS * 2))
        self.heartbeat_stop = None
        self.heartbeat_thread = None
        if not self.acquired:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            return
        self.acquired = False
        try:
            if self.lock_path is not None and self.metadata is not None:
                _unlink_runner_lock_if_owned(
                    self.lock_path,
                    fd=self.fd,
                    lock_id=str(self.metadata.get("lock_id") or ""),
                )
        finally:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def _start_heartbeat(self) -> None:
        if self.lock_path is None or self.metadata is None or self.fd is None:
            return
        self.heartbeat_stop = threading.Event()
        self.heartbeat_thread = threading.Thread(
            target=_runner_lock_heartbeat_loop,
            args=(self.lock_path, self.fd, self.metadata, self.heartbeat_stop),
            name=f"loopplane-runner-lock-{self.metadata.get('lock_key')}",
            daemon=True,
        )
        self.heartbeat_thread.start()

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
    advisory_state = _runner_lock_advisory_state(path)
    if advisory_state == "absent" or (advisory_state == "unknown" and not path.exists()):
        return {
            **base,
            "state": RUNNER_LOCK_ABSENT,
            "ok": True,
            "message": f"No machine runner lock is currently held for {key}.",
            "guidance": "",
        }
    if advisory_state == "held":
        return {
            **base,
            "state": RUNNER_LOCK_ACTIVE,
            "ok": True,
            "advisory_lock_held": True,
            "message": f"Machine runner lock {key} is held by an active advisory-lock owner.",
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
    owner_host = str(metadata.get("hostname") or "").strip()
    host_is_local = process_host_is_local(owner_host or None)
    pid_alive = _pid_exists(pid) if host_is_local is not False else None
    summary = _metadata_summary(metadata, pid_alive=pid_alive, heartbeat_age=heartbeat_age, acquired_age=acquired_age)

    if host_is_local is False:
        ttl_seconds = _positive_int(
            metadata.get("lease_ttl_seconds"),
            default=RUNNER_LOCK_LEASE_TTL_SECONDS,
        )
        if heartbeat_age is not None and heartbeat_age <= ttl_seconds:
            return {
                **base,
                **summary,
                "state": RUNNER_LOCK_ACTIVE,
                "ok": True,
                "message": f"Machine runner lock {key} is held by a remotely heartbeating process.",
                "guidance": "",
            }
        guidance = _stale_lock_guidance(path)
        return {
            **base,
            **summary,
            "state": RUNNER_LOCK_STALE,
            "ok": False,
            "message": f"Machine runner lock {key} has an expired remote heartbeat. {guidance}",
            "guidance": guidance,
        }

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
    metadata = {
        "schema_version": RUNNER_LOCK_SCHEMA_VERSION,
        "lock_id": uuid.uuid4().hex,
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
        "hostname": socket.gethostname(),
        "process_start_time": _process_start_time(os.getpid()),
        "acquired_at": now,
        "heartbeat_at": now,
        "lease_ttl_seconds": RUNNER_LOCK_LEASE_TTL_SECONDS,
        "lease_expires_at": _timestamp_after(now, RUNNER_LOCK_LEASE_TTL_SECONDS),
    }
    env = getattr(adapter_input, "env", {})
    if isinstance(env, Mapping):
        metadata["project_root"] = env.get("LOOPPLANE_PROJECT_ROOT")
        metadata["active_run_lease_path"] = env.get("LOOPPLANE_ACTIVE_RUN_LEASE")
    return metadata


def _runner_lock_heartbeat_loop(
    lock_path: Path,
    fd: int,
    metadata: dict[str, Any],
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(RUNNER_LOCK_HEARTBEAT_INTERVAL_SECONDS):
        now = _utc_timestamp()
        metadata["heartbeat_at"] = now
        metadata["lease_expires_at"] = _timestamp_after(now, RUNNER_LOCK_LEASE_TTL_SECONDS)
        if not _replace_runner_lock_if_owned(lock_path, fd=fd, metadata=metadata):
            return


def _replace_runner_lock_if_owned(lock_path: Path, *, fd: int, metadata: Mapping[str, Any]) -> bool:
    lock_id = str(metadata.get("lock_id") or "")
    if not lock_id or not _runner_lock_fd_matches_path(fd, lock_path):
        return False
    try:
        current, error = _read_lock_metadata_fd(fd)
        if error is not None or current is None or str(current.get("lock_id") or "") != lock_id:
            return False
        _write_runner_lock_fd(fd, metadata)
        return True
    except OSError:
        return False


def _unlink_runner_lock_if_owned(lock_path: Path, *, fd: int | None, lock_id: str) -> bool:
    if fd is None or not _runner_lock_fd_matches_path(fd, lock_path):
        return False
    metadata, error = _read_lock_metadata_fd(fd)
    if error is None and metadata is not None and (
        not lock_id or str(metadata.get("lock_id") or "") != lock_id
    ):
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return True


def _reclaim_stale_runner_lock(lock_path: Path, adapter_input: Any) -> bool:
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        if not _acquire_runner_file_lock(fd, blocking=False):
            return False
        if not _runner_lock_fd_matches_path(fd, lock_path):
            return False
        metadata, error = _read_lock_metadata_fd(fd)
        if error is not None or metadata is None or not _runner_lock_owner_is_stale(metadata, adapter_input):
            return False
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        return True
    finally:
        os.close(fd)


def _runner_lock_owner_is_stale(metadata: Mapping[str, Any], adapter_input: Any) -> bool:
    lease_status = _owner_active_run_lease_status(metadata, adapter_input)
    if lease_status is not None:
        return lease_status

    heartbeat = _parse_timestamp(metadata.get("heartbeat_at") or metadata.get("acquired_at"))
    if heartbeat is None:
        return False
    ttl_seconds = _positive_int(
        metadata.get("lease_ttl_seconds"),
        default=RUNNER_LOCK_LEASE_TTL_SECONDS,
    )
    heartbeat_expired = _age_seconds(datetime.now(UTC), heartbeat) > ttl_seconds
    owner_host = str(metadata.get("hostname") or "").strip()
    if owner_host and process_host_is_local(owner_host) is True:
        pid = _positive_int(metadata.get("pid"), default=-1)
        if _pid_exists(pid) is not True:
            return True
        expected_start = str(metadata.get("process_start_time") or "").strip()
        observed_start = _process_start_time(pid)
        return bool(expected_start and observed_start and expected_start != observed_start)
    if owner_host:
        return heartbeat_expired
    return False


def _owner_active_run_lease_status(metadata: Mapping[str, Any], adapter_input: Any) -> bool | None:
    lease_path_value = str(metadata.get("active_run_lease_path") or "").strip()
    env = getattr(adapter_input, "env", {})
    if not lease_path_value and isinstance(env, Mapping):
        same_workflow = str(metadata.get("workflow_id") or "") == str(getattr(adapter_input, "workflow_id", ""))
        runtime_dir = str(env.get("LOOPPLANE_RUNTIME_DIR") or "").strip()
        run_id = str(metadata.get("run_id") or "").strip()
        if same_workflow and runtime_dir and run_id:
            lease_path_value = (Path(runtime_dir) / "active_run_leases" / f"{run_id}.json").as_posix()
    if not lease_path_value:
        return None
    lease_path = Path(lease_path_value)
    try:
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(lease, Mapping):
        return None
    status = str(lease.get("status") or "").strip().lower()
    if status in {"completed", "failed", "released", "cancelled", "timed_out", "stale"}:
        return True
    expires_at = _parse_timestamp(lease.get("lease_expires_at"))
    if expires_at is not None and expires_at < datetime.now(UTC):
        return True
    if status in {"prepared", "running"}:
        return False
    return None


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


def _timestamp_after(timestamp: str, seconds: int) -> str:
    parsed = _parse_timestamp(timestamp) or datetime.now(UTC)
    return datetime.fromtimestamp(parsed.timestamp() + max(1, seconds), UTC).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _process_start_time(pid: int) -> str | None:
    return read_process_start_time(pid)


def _acquire_runner_file_lock(fd: int, *, blocking: bool) -> bool:
    if fcntl is None:
        raise RunnerResourceLockError(
            "machine-scoped runner locks require POSIX advisory file locking support"
        )
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(fd, operation)
    except BlockingIOError:
        return False
    except OSError as error:
        raise RunnerResourceLockError(f"unable to acquire machine runner advisory lock: {error}") from error
    return True


def _runner_lock_fd_matches_path(fd: int, path: Path) -> bool:
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = path.stat()
    except OSError:
        return False
    return (descriptor_stat.st_dev, descriptor_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)


def _runner_lock_advisory_state(path: Path) -> str:
    """Return whether the lock inode is actively held without reading mutable metadata."""

    if fcntl is None:
        return "unknown"
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return "held"
        except OSError:
            return "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "unlocked"
    finally:
        os.close(fd)


def _write_runner_lock_fd(fd: int, metadata: Mapping[str, Any]) -> None:
    encoded = (json.dumps(dict(metadata), indent=2, sort_keys=True) + "\n").encode("utf-8")
    offset = 0
    while offset < len(encoded):
        written = os.pwrite(fd, encoded[offset:], offset)
        if written <= 0:
            raise OSError("short write while updating runner resource lock")
        offset += written
    os.ftruncate(fd, len(encoded))
    os.fsync(fd)


def _read_lock_metadata_fd(fd: int) -> tuple[dict[str, Any] | None, str | None]:
    try:
        size = os.fstat(fd).st_size
        encoded = os.pread(fd, max(1, size), 0)
        data = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, f"invalid JSON lock metadata: {error}"
    except OSError as error:
        return None, str(error)
    if not isinstance(data, Mapping):
        return None, "expected JSON object"
    return dict(data), None


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
        "lock_id": metadata.get("lock_id"),
        "hostname": metadata.get("hostname"),
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
        "lease_ttl_seconds": metadata.get("lease_ttl_seconds"),
        "lease_expires_at": metadata.get("lease_expires_at"),
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
    return process_pid_exists(pid)
