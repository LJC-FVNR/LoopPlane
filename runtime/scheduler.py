from __future__ import annotations

import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from hashlib import sha256
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from runtime.adapters.base import (
    AdapterContractError,
    AdapterInput,
    AdapterOutput,
    AdapterOutputPaths,
    utc_timestamp,
    write_adapter_result,
)
from runtime.adapters.registry import AdapterLookupError, get_adapter
from runtime.agent_status import is_success_worker_status, normalize_worker_status
from runtime.agent_runners import AgentRunnerConfigError, RunnerConfig, load_agent_runners
from runtime.approval import (
    APPROVAL_REQUESTS_FILENAME,
    APPROVAL_RESPONSES_FILENAME,
    approval_record_status,
    default_expires_at,
    load_approval_policy,
    new_approval_id,
    task_approval_decision,
)
from runtime.control import (
    CONTROL_REQUESTS_FILENAME,
    CONTROL_RESPONSES_FILENAME,
    CONTROL_REQUEST_TYPES,
    control_record_id,
)
from runtime.exit_codes import (
    ADAPTER_COMMAND_UNAVAILABLE_EXIT_CODE,
    ADAPTER_POLICY_BLOCKED_EXIT_CODE,
    EXIT_DUPLICATE_SCHEDULER as DOCUMENTED_EXIT_DUPLICATE_SCHEDULER,
    EXIT_FAILURE_BUDGET_EXHAUSTED,
    EXIT_FINAL_VERIFICATION_FAILED,
    EXIT_GENERIC_FAILURE,
    EXIT_INVALID_CONFIG,
    EXIT_MIGRATION_REQUIRED,
    EXIT_NEEDS_HUMAN,
    EXIT_PLAN_MALFORMED,
    EXIT_RUNNER_UNAVAILABLE,
    EXIT_SECURITY_POLICY_VIOLATION,
    EXIT_SUCCESS,
    EXIT_VALIDATION_FAILED,
    EXIT_WAITING_APPROVAL,
    EXIT_WAITING_BACKGROUND_JOB,
    has_text,
)
from runtime.path_resolution import WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.plan_objectives import (
    DEFAULT_OBJECTIVE_MAX_EXPANSIONS,
    ObjectiveRecord,
    objective_closure_fingerprint,
    objective_structure_fingerprint,
    parse_plan_objectives,
)
from runtime.planning import inspect_active_plan
from runtime.prompt_builder import PromptBuildError, build_prompt_for_prepared_run
from runtime.schema_validation import check_project_schema_version, schema_validation_exit_code
from runtime.version_control import capture_run_git_metadata, create_git_checkpoint
from runtime.workspace_boundary_policy import evaluate_worker_write_boundary, worker_write_boundary_message
from runtime.workflow_lifecycle import (
    mark_workflow_completed,
    mark_workflow_objective_unresolved,
    mark_workflow_runtime_status,
)


SCHEMA_VERSION = "1.5"
EXIT_DUPLICATE_SCHEDULER = DOCUMENTED_EXIT_DUPLICATE_SCHEDULER
SCHEDULER_LOCK_TTL_SECONDS = 120
ACTIVE_RUN_LEASE_TTL_SECONDS = 120
ACTIVE_RUN_LEASE_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_RUN_GIT_METADATA_DETAIL_LEVEL = "status"
EMIT_ADAPTER_COMPLETED_EVENTS = False
EVENTS_SEGMENT = "events_000001.jsonl"
EVENTS_MANIFEST_FILENAME = "manifest.json"
EVENT_PAYLOAD_SIDECAR_DIR = "event_payloads"
EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES = 8192
EVENT_SNAPSHOT_INTERVAL = 100
COLLAPSIBLE_SCHEDULER_WAIT_ACTIONS = frozenset(
    {
        "wait_paused",
        "wait_stopped",
        "wait_approval",
        "wait_background_job",
        "wait_config",
        "wait_no_executable_work",
        "wait_runner_availability",
    }
)
OWNER_FILENAME = "owner.json"
ADAPTER_INPUT_FILENAME = "adapter_input.json"
RUN_EXECUTION_FILENAME = "run_execution.json"
NODE_SUMMARY_FILENAME = "node_summary.json"
FAILURE_REGISTRY_FILENAME = "failure_registry.json"
EXPANSION_PROPOSAL_FILENAME = "expansion_proposal.json"
RUNNER_HEALTH_FILENAME = "runner_health.json"
RUNNER_HEALTH_EVENT_LIMIT = 200
FAILURE_TERMINAL_STATUSES = frozenset({"recovered", "waived", "exhausted", "needs_human"})
DEFAULT_MAX_RECOVERY_ATTEMPTS = 1
# Failure classes for non-task-keyed scheduler "action" runs (objective verifier
# and self-expansion planner). These are recorded in the same failure_registry as
# worker failures so they get the same bounded auto-retry, but they are keyed by
# the action/run scope (not a task_id) and are re-selected as the SAME action
# rather than as a task-keyed recovery_worker.
ACTION_FAILURE_CLASSES = frozenset({"objective_verifier_failed", "expansion_planner_failed"})
# Run statuses that mean the run failed to EXECUTE (the agent crashed / hit an API
# error / produced no usable result) as opposed to a successful run that returned a
# normal "unmet" verdict. Only these are auto-retried for action runs.
TRANSIENT_RUN_FAILURE_STATUSES = frozenset({"failed_agent", "failed_system"})
BACKGROUND_JOBS_FILENAME = "background_jobs.json"
BACKGROUND_JOB_HEARTBEAT_TTL_SECONDS = 600
BACKGROUND_JOB_PID_STARTUP_GRACE_SECONDS = 15
BACKGROUND_REGISTRY_LOCK_TTL_SECONDS = 30
BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS = 35.0
ALLOWED_BACKGROUND_JOB_STATUSES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "stale",
        "needs_recovery",
    }
)
BACKGROUND_JOB_SAFE_STATUSES = frozenset({"completed", "cancelled"})
BACKGROUND_JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out", "cancelled"})
BACKGROUND_JOB_ATTENTION_STATUSES = frozenset({"failed", "timed_out", "stale", "needs_recovery"})
ACTIVE_RUN_LEASE_ACTIVE_STATUSES = frozenset({"starting", "running", "pending", "waiting"})
ACTIVE_RUN_LEASE_INACTIVE_STATUSES = frozenset({"completed", "succeeded", "failed", "cancelled", "aborted", "released"})
ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME = "active_run_leases_fingerprint.json"


class SchedulerError(RuntimeError):
    pass


class SchedulerLockError(SchedulerError):
    pass


@dataclass(frozen=True)
class AtomicOwnerLock:
    lock_dir: Path
    owner: str
    ttl_seconds: int = SCHEDULER_LOCK_TTL_SECONDS

    @property
    def owner_path(self) -> Path:
        return self.lock_dir / OWNER_FILENAME

    def acquire(
        self,
        *,
        timeout_seconds: float = 0.0,
        poll_interval_seconds: float = 0.05,
    ) -> "HeldOwnerLock":
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                return self._acquire_once()
            except SchedulerLockError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(max(0.001, float(poll_interval_seconds)), remaining))

    def _acquire_once(self) -> "HeldOwnerLock":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        metadata = _owner_metadata(self.owner, self.ttl_seconds)
        encoded = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")
        last_error: FileExistsError | None = None
        for attempt in range(2):
            try:
                fd = os.open(self.owner_path, flags, 0o600)
                break
            except FileExistsError as error:
                last_error = error
                if attempt == 0 and self._reclaim_stale_owner():
                    continue
                raise SchedulerLockError(f"lock is already held: {self.owner_path}") from error
        else:  # pragma: no cover - loop always raises or breaks.
            raise SchedulerLockError(f"lock is already held: {self.owner_path}") from last_error
        try:
            _write_all(fd, encoded)
            os.fsync(fd)
        except BaseException:
            _unlink_owned_lock_path(self.owner_path, fd=fd, lock_id=None)
            os.close(fd)
            raise
        return HeldOwnerLock(self, fd, metadata)

    def _reclaim_stale_owner(self) -> bool:
        try:
            observed_stat = self.owner_path.stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        owner = _read_json_object(self.owner_path, default={})
        if not isinstance(owner, Mapping):
            return False
        heartbeat = _parse_iso_timestamp(owner.get("heartbeat_at") or owner.get("started_at"))
        if heartbeat is None:
            heartbeat = datetime.fromtimestamp(observed_stat.st_mtime, UTC)
        ttl_seconds = max(1, _int_value(owner.get("ttl_seconds"), default=self.ttl_seconds))
        age_seconds = max(0, int((datetime.now(UTC) - heartbeat).total_seconds()))
        if age_seconds <= ttl_seconds:
            return False
        owner_host = _non_empty_text(owner.get("hostname"))
        if owner_host is not None and _hostnames_match(owner_host, socket.gethostname()):
            pid = _lock_owner_pid(owner)
            if pid is not None and _pid_exists(pid) is True:
                expected_start = _non_empty_text(owner.get("process_start_time"))
                observed_start = _process_start_time(pid)
                if not (
                    expected_start is not None
                    and expected_start.startswith("proc:")
                    and observed_start is not None
                    and observed_start != expected_start
                ):
                    return False
        return _unlink_lock_path_if_unchanged(
            self.owner_path,
            observed_stat=observed_stat,
            lock_id=_non_empty_text(owner.get("lock_id")),
            heartbeat_at=_non_empty_text(owner.get("heartbeat_at")),
        )


class HeldOwnerLock:
    def __init__(self, lock: AtomicOwnerLock, fd: int, metadata: Mapping[str, Any]) -> None:
        self.lock = lock
        self.fd = fd
        self.metadata = dict(metadata)
        self.lock_id = str(metadata["lock_id"])
        self.released = False
        self._heartbeat_error: SchedulerLockError | None = None
        self._metadata_lock = threading.Lock()

    def __enter__(self) -> "HeldOwnerLock":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def heartbeat(self) -> None:
        with self._metadata_lock:
            if self.released:
                return
            if self._heartbeat_error is not None:
                raise self._heartbeat_error
            try:
                self._assert_current_owner()
                self.metadata["heartbeat_at"] = utc_timestamp()
                encoded = (json.dumps(self.metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")
                os.lseek(self.fd, 0, os.SEEK_SET)
                os.ftruncate(self.fd, 0)
                _write_all(self.fd, encoded)
                os.fsync(self.fd)
                self._assert_current_owner()
            except (OSError, SchedulerLockError) as error:
                if isinstance(error, SchedulerLockError):
                    lock_error = error
                else:
                    lock_error = SchedulerLockError(f"lock heartbeat failed: {self.lock.owner_path}: {error}")
                self._heartbeat_error = lock_error
                raise lock_error from error

    def _assert_current_owner(self) -> None:
        try:
            descriptor_stat = os.fstat(self.fd)
            path_stat = self.lock.owner_path.stat()
        except OSError as error:
            raise SchedulerLockError(f"lock ownership was lost: {self.lock.owner_path}") from error
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise SchedulerLockError(f"lock ownership was replaced: {self.lock.owner_path}")
        owner = _read_json_object(self.lock.owner_path, default={})
        if not isinstance(owner, Mapping) or str(owner.get("lock_id") or "") != self.lock_id:
            raise SchedulerLockError(f"lock ownership token changed: {self.lock.owner_path}")

    @contextmanager
    def keepalive(self, *, interval_seconds: float | None = None):
        interval = interval_seconds
        if interval is None:
            interval = min(30.0, max(1.0, float(self.lock.ttl_seconds) / 3.0))
        stop = threading.Event()

        def _run() -> None:
            while not stop.wait(interval):
                try:
                    self.heartbeat()
                except (OSError, SchedulerLockError):
                    return

        thread = threading.Thread(target=_run, name="loopplane-scheduler-lock-heartbeat", daemon=True)
        thread.start()
        try:
            yield self
        finally:
            stop.set()
            thread.join(timeout=max(1.0, interval + 1.0))

    def release(self) -> None:
        with self._metadata_lock:
            if self.released:
                return
            self.released = True
            try:
                _unlink_owned_lock_path(self.lock.owner_path, fd=self.fd, lock_id=self.lock_id)
            finally:
                os.close(self.fd)


@dataclass(frozen=True)
class SchedulerContext:
    project: Path
    workflow_id: str
    workflow_config: Mapping[str, Any]
    paths: WorkflowPaths


@dataclass(frozen=True)
class PreparedRun:
    workflow_id: str
    run_id: str
    node_id: str
    role: str
    runner_id: str
    task_id: str | None
    scheduler_run_dir: Path
    role_output_dir: Path
    task_evidence_run_dir: Path | None
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    final_output_path: Path
    adapter_result_path: Path
    active_run_lease_path: Path
    prepared_at: str
    scheduler_owner: str
    blocks_scheduler: bool = True
    updates_runtime_state: bool = True

    def to_dict(self, *, project_root: Path | None = None) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "role": self.role,
            "runner_id": self.runner_id,
            "task_id": self.task_id,
            "scheduler_run_dir": _path_for_record(project_root, self.scheduler_run_dir),
            "role_output_dir": _path_for_record(project_root, self.role_output_dir),
            "task_evidence_run_dir": (
                _path_for_record(project_root, self.task_evidence_run_dir)
                if self.task_evidence_run_dir is not None
                else None
            ),
            "prompt_path": _path_for_record(project_root, self.prompt_path),
            "stdout_path": _path_for_record(project_root, self.stdout_path),
            "stderr_path": _path_for_record(project_root, self.stderr_path),
            "final_output_path": _path_for_record(project_root, self.final_output_path),
            "adapter_result_path": _path_for_record(project_root, self.adapter_result_path),
            "active_run_lease_path": _path_for_record(project_root, self.active_run_lease_path),
            "prepared_at": self.prepared_at,
            "scheduler_owner": self.scheduler_owner,
            "blocks_scheduler": self.blocks_scheduler,
            "updates_runtime_state": self.updates_runtime_state,
        }


def prepare_run(
    project_root: Path | str,
    *,
    role: str,
    task_id: str | None = None,
    runner_id: str | None = None,
    scheduler_owner: str | None = None,
    lease_ttl_seconds: int = ACTIVE_RUN_LEASE_TTL_SECONDS,
    blocks_scheduler: bool = True,
    updates_runtime_state: bool = True,
    append_prepared_event: bool = True,
) -> PreparedRun:
    project = Path(project_root).expanduser().resolve()
    context_result = load_scheduler_context(project)
    if not context_result["ok"]:
        raise SchedulerError(str(context_result["message"]))
    context: SchedulerContext = context_result["context"]
    paths = context.paths
    runner_config = _select_runner_for_prepare(
        project,
        workflow_config=context.workflow_config,
        role=role,
        runner_id=runner_id,
    )

    prepared_at = utc_timestamp()
    owner = scheduler_owner or _scheduler_owner()
    run_id = _new_runtime_run_id(paths)
    scheduler_run_dir = paths.runtime_dir / "runs" / run_id
    role_output_dir = _role_output_dir(paths, role=role, task_id=task_id, run_id=run_id)
    task_evidence_run_dir = role_output_dir if _role_uses_task_evidence(role) else None
    node_id = _node_id(role=role, task_id=task_id, run_id=run_id)
    output_paths = AdapterOutputPaths.for_scheduler_run_dir(scheduler_run_dir)
    active_run_lease_path = paths.runtime_dir / "active_run_leases" / f"{run_id}.json"

    scheduler_run_dir.mkdir(parents=True, exist_ok=False)
    role_output_dir.mkdir(parents=True, exist_ok=False)
    if task_evidence_run_dir is not None:
        for child in ("logs", "artifacts", "raw"):
            (task_evidence_run_dir / child).mkdir(parents=True, exist_ok=True)
        (scheduler_run_dir / "task_id.txt").write_text(f"{task_id}\n", encoding="utf-8")

    prepared = PreparedRun(
        workflow_id=context.workflow_id,
        run_id=run_id,
        node_id=node_id,
        role=role,
        runner_id=runner_config.runner_id,
        task_id=task_id,
        scheduler_run_dir=scheduler_run_dir,
        role_output_dir=role_output_dir,
        task_evidence_run_dir=task_evidence_run_dir,
        prompt_path=scheduler_run_dir / "prompt.md",
        stdout_path=output_paths.stdout_path,
        stderr_path=output_paths.stderr_path,
        final_output_path=output_paths.final_output_path,
        adapter_result_path=output_paths.adapter_result_path,
        active_run_lease_path=active_run_lease_path,
        prepared_at=prepared_at,
        scheduler_owner=owner,
        blocks_scheduler=blocks_scheduler,
        updates_runtime_state=updates_runtime_state,
    )
    metadata = prepared.to_dict(project_root=project)
    _write_json(scheduler_run_dir / "run_metadata.json", metadata)
    _write_json(role_output_dir / "metadata.json", metadata)

    lease_record = {
        **metadata,
        "status": "starting",
        "heartbeat_at": prepared_at,
        "lease_expires_at": _timestamp_after(prepared_at, lease_ttl_seconds),
        "scheduler_pid": os.getpid(),
        "adapter_pid": None,
        "adapter_child_pid": None,
        "lease_ttl_seconds": lease_ttl_seconds,
    }
    _write_active_run_lease(active_run_lease_path, lease_record)
    if updates_runtime_state:
        _update_runtime_state(
            paths,
            status="preparing_run",
            scheduler_update={
                "last_action": "prepare_run",
                "owner": owner,
                "active_run_id": run_id,
                "active_node_id": node_id,
                "active_task_id": task_id,
                "active_role": role,
                "active_runner_id": runner_config.runner_id,
            },
        )
    if append_prepared_event:
        append_event(
            paths,
            workflow_id=context.workflow_id,
            event_type="run_prepared",
            run_id=run_id,
            data={
                "owner": owner,
                "node_id": node_id,
                "role": role,
                "runner_id": runner_config.runner_id,
                "task_id": task_id,
                "scheduler_run_dir": metadata["scheduler_run_dir"],
                "role_output_dir": metadata["role_output_dir"],
                "task_evidence_run_dir": metadata["task_evidence_run_dir"],
                "active_run_lease_path": metadata["active_run_lease_path"],
                "blocks_scheduler": blocks_scheduler,
                "updates_runtime_state": updates_runtime_state,
            },
        )
    return prepared


def run_scheduler(
    project_root: Path | str,
    *,
    max_ticks: int = 1,
    lease_heartbeat_interval_seconds: float = ACTIVE_RUN_LEASE_HEARTBEAT_INTERVAL_SECONDS,
    continue_after_final_verification: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    context_result = load_scheduler_context(project)
    if not context_result["ok"]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "exit_code": int(context_result.get("exit_code", EXIT_INVALID_CONFIG)),
            "message": context_result["message"],
            "project_root": project.as_posix(),
            "workflow_id": None,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "selected_action": None,
        }

    context: SchedulerContext = context_result["context"]
    owner = _scheduler_owner()
    lock = AtomicOwnerLock(context.paths.runtime_dir / "lock" / "scheduler_instance_lock", owner)
    try:
        held_lock = lock.acquire()
    except SchedulerLockError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "duplicate_scheduler",
            "exit_code": EXIT_DUPLICATE_SCHEDULER,
            "message": str(error),
            "project_root": project.as_posix(),
            "workflow_id": context.workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "selected_action": None,
        }

    ticks = max(1, int(max_ticks))
    ticks_run = 0
    selected: dict[str, Any] | None = None
    action_history: list[dict[str, Any]] = []
    with held_lock, held_lock.keepalive():
        completion_snapshot = load_scheduler_snapshot(project)
        completion_selected = select_next_action(completion_snapshot)
        if completion_selected.get("action") == "complete":
            selected = completion_selected
            execution_result = execute_selected_action(
                completion_snapshot,
                selected,
                owner=owner,
                lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
            )
            if execution_result is not None:
                selected["execution_result"] = execution_result
                selected["ok"] = bool(execution_result.get("ok", selected.get("ok", True)))
            status = "ok" if selected.get("ok", True) else "failed"
            stopped = _scheduler_stop_summary(project, selected, ticks_requested=ticks, ticks_run=ticks_run)
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": status == "ok",
                "status": status,
                "exit_code": _scheduler_exit_code_for_selected(selected, ok=status == "ok"),
                "message": selected["reason"],
                "project_root": project.as_posix(),
                "workflow_id": context.workflow_id,
                "started_at": started_at,
                "ended_at": utc_timestamp(),
                "ticks_requested": ticks,
                "ticks_run": ticks_run,
                "stopped_reason": stopped["stopped_reason"],
                "pending_tasks": stopped["pending_tasks"],
                "hint": stopped.get("hint"),
                "action_history": action_history,
                "selected_action": selected,
            }
        emitted_scheduler_started = False
        collapsed_wait_tick = False
        for tick_index in range(ticks):
            held_lock.heartbeat()
            validation_failures = _ingest_failed_validations(context.paths, workflow_id=context.workflow_id)
            snapshot = load_scheduler_snapshot(project)
            background_failures = _ingest_failed_background_jobs(
                context.paths,
                workflow_id=context.workflow_id,
                background_jobs=snapshot.get("background_jobs", []),
                tasks=snapshot.get("tasks", []),
            )
            if background_failures:
                snapshot = load_scheduler_snapshot(project)
            _record_manual_plan_change_if_needed(snapshot, owner=owner)
            selected = select_next_action(snapshot)
            selected_action_name = str(selected.get("action") or "")
            collapse_wait_tick = (
                ticks == 1
                and not validation_failures
                and not background_failures
                and _is_collapsible_scheduler_wait_action(selected)
            )
            if not collapse_wait_tick:
                if not emitted_scheduler_started:
                    append_event(
                        context.paths,
                        workflow_id=context.workflow_id,
                        event_type="scheduler_started",
                        data={"owner": owner, "max_ticks": ticks},
                    )
                    emitted_scheduler_started = True
                append_event(
                    context.paths,
                    workflow_id=context.workflow_id,
                    event_type="scheduler_tick",
                    data={"owner": owner, "tick_index": tick_index + 1},
                )
            if validation_failures:
                append_event(
                    context.paths,
                    workflow_id=context.workflow_id,
                    event_type="failure_registry_updated",
                    data={
                        "owner": owner,
                        "source": "validation_ingest",
                        "failure_ids": [failure["failure_id"] for failure in validation_failures],
                    },
                )
            if background_failures:
                append_event(
                    context.paths,
                    workflow_id=context.workflow_id,
                    event_type="failure_registry_updated",
                    data={
                        "owner": owner,
                        "source": "background_job_ingest",
                        "failure_ids": [
                            failure["failure_id"] for failure in background_failures
                        ],
                    },
                )
            if selected_action_name != "complete" and not collapse_wait_tick:
                append_event(
                    context.paths,
                    workflow_id=context.workflow_id,
                    event_type="scheduler_action_selected",
                    data={
                        "owner": owner,
                        "action": selected["action"],
                        "selected": selected.get("selected"),
                        "would_wait": selected["would_wait"],
                        "reason": selected["reason"],
                    },
                )
            execution_result = execute_selected_action(
                snapshot,
                selected,
                owner=owner,
                lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
                emit_wait_event=not collapse_wait_tick,
            )
            ticks_run += 1
            if execution_result is not None:
                selected["execution_result"] = execution_result
                selected["ok"] = bool(execution_result.get("ok", selected.get("ok", True)))
            action_history.append(_scheduler_action_history_entry(selected, tick_index=tick_index + 1))
            if collapse_wait_tick:
                collapsed_wait_tick = True
                append_event(
                    context.paths,
                    workflow_id=context.workflow_id,
                    event_type="scheduler_wait_tick",
                    data={
                        "owner": owner,
                        "action": selected_action_name,
                        "status": _scheduler_wait_status_for_action(selected_action_name),
                        "reason": selected.get("reason"),
                        "selected": _json_safe_object(selected.get("selected", {})),
                        "would_wait": selected.get("would_wait"),
                        "blocking_conditions": list(selected.get("blocking_conditions") or []),
                        "tick_index": tick_index + 1,
                        "ticks": ticks,
                        "ticks_run": ticks_run,
                    },
                )
            if not _scheduler_should_continue_after_tick(
                selected,
                tick_index=tick_index,
                max_ticks=ticks,
                continue_after_final_verification=continue_after_final_verification,
            ):
                break
        final_verification_completed = (
            selected is not None
            and selected.get("action") == "run_final_verification"
            and isinstance(selected.get("execution_result"), Mapping)
            and selected["execution_result"].get("pass") is True
        )
        completion_selected = selected is not None and selected.get("action") == "complete"
        stopped = _scheduler_stop_summary(project, selected, ticks_requested=ticks, ticks_run=ticks_run)
        if not final_verification_completed and not completion_selected and not collapsed_wait_tick:
            append_event(
                context.paths,
                workflow_id=context.workflow_id,
                event_type="scheduler_exited",
                data={
                    "owner": owner,
                    "ticks": ticks,
                    "ticks_run": ticks_run,
                    "last_action": selected["action"] if selected else None,
                    "stopped_reason": stopped["stopped_reason"],
                    "pending_tasks": stopped["pending_tasks"],
                },
            )

    status = "ok" if selected and selected.get("ok", True) else "failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": status == "ok",
        "status": status,
        "exit_code": _scheduler_exit_code_for_selected(selected, ok=status == "ok"),
        "message": selected["reason"] if selected else "Scheduler did not select an action.",
        "project_root": project.as_posix(),
        "workflow_id": context.workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "ticks_requested": ticks,
        "ticks_run": ticks_run,
        "stopped_reason": stopped["stopped_reason"],
        "pending_tasks": stopped["pending_tasks"],
        "hint": stopped.get("hint"),
        "action_history": action_history,
        "selected_action": selected,
    }


def _action_failure_has_budget_for_retry(action: str, execution: Mapping[str, Any]) -> bool:
    """True when a verifier/planner RUN failed transiently but was registered as a
    recoverable action failure that still has recovery budget left. Such a tick must
    keep the loop running so the same action is immediately re-selected and retried."""
    if action not in {"run_phase_objective_verifier", "run_final_objective_verifier", "run_expansion_planner"}:
        return False
    update = execution.get("failure_registry_update")
    if not isinstance(update, Mapping):
        return False
    if str(update.get("failure_class") or "") not in ACTION_FAILURE_CLASSES:
        return False
    if str(update.get("status") or "") in FAILURE_TERMINAL_STATUSES:
        return False
    return bool(update.get("budget_remaining"))


def _scheduler_should_continue_after_tick(
    selected: Mapping[str, Any],
    *,
    tick_index: int,
    max_ticks: int,
    continue_after_final_verification: bool = False,
) -> bool:
    if tick_index + 1 >= max_ticks:
        return False
    action = str(selected.get("action") or "")
    if action == "handle_control_request":
        return True
    if action in {
        "complete",
        "wait_paused",
        "wait_stopped",
        "wait_approval",
        "requires_attention",
        "wait_background_job",
        "wait_config",
        "wait_no_executable_work",
        "wait_runner_availability",
    }:
        return False
    if selected.get("would_wait"):
        return False
    execution = selected.get("execution_result")
    # A transient objective-verifier / expansion-planner RUN failure that was
    # registered as a recoverable action failure with budget remaining should let
    # the loop CONTINUE so the same action is immediately re-selected and retried.
    # This is checked before the generic ok/status terminal handling below so a
    # failed-but-recoverable action run does not stop the loop. When the budget is
    # exhausted the helper returns False and we fall through to stopping the loop so
    # a genuinely broken run still surfaces to the human.
    if isinstance(execution, Mapping) and _action_failure_has_budget_for_retry(action, execution):
        return True
    if selected.get("ok") is False:
        return False
    if isinstance(execution, Mapping):
        if execution.get("ok") is False:
            return False
        if action == "run_final_verification" and execution.get("pass") is True:
            return bool(continue_after_final_verification)
        if str(execution.get("next_step") or "") in {"waiting_background", "needs_human", "recovery_pending"}:
            return False
        if str(execution.get("status") or "") in {"running_background", "waiting_config", "needs_human", "failed_agent", "failed_system", "failed_validation"}:
            return False
    return action in {
        "run_worker",
        "run_recovery",
        "run_phase_objective_verifier",
        "run_final_objective_verifier",
        "run_expansion_planner",
        "resolve_expansion_failure",
        "run_final_verification",
    }


def _is_collapsible_scheduler_wait_action(selected: Mapping[str, Any]) -> bool:
    action = str(selected.get("action") or "")
    return action in COLLAPSIBLE_SCHEDULER_WAIT_ACTIONS


def _scheduler_wait_status_for_action(action_name: str) -> str:
    return {
        "wait_paused": "paused",
        "wait_stopped": "stopped",
        "wait_approval": "waiting_approval",
        "requires_attention": "requires_attention",
        "wait_background_job": "waiting_background_job",
        "wait_config": "waiting_config",
        "wait_no_executable_work": "waiting",
        "wait_runner_availability": "waiting_runner_availability",
    }[action_name]


def _scheduler_stop_summary(
    project: Path,
    selected: Mapping[str, Any] | None,
    *,
    ticks_requested: int,
    ticks_run: int,
) -> dict[str, Any]:
    selected_action = str(selected.get("action") or "") if isinstance(selected, Mapping) else ""
    pending_tasks = _pending_task_count_after_run(project)
    stopped_reason = "selected_action_terminal"
    hint: str | None = None
    if selected is None:
        stopped_reason = "no_action_selected"
    elif selected_action == "requires_attention":
        stopped_reason = selected_action
    elif selected_action.startswith("wait_"):
        stopped_reason = selected_action
    elif ticks_run >= ticks_requested and selected_action not in {"complete", "wait_paused", "wait_stopped"}:
        stopped_reason = "max_ticks_reached"
        if pending_tasks:
            hint = "Additional tasks remain; run again with a larger --max-ticks value or use --until-complete."
    return {
        "stopped_reason": stopped_reason,
        "pending_tasks": pending_tasks,
        "hint": hint,
    }


def _pending_task_count_after_run(project: Path) -> int:
    try:
        snapshot = load_scheduler_snapshot(project)
    except Exception:
        return 0
    count = 0
    for task in snapshot.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        status = str(task.get("status") or " ")
        if status not in {"x", "-"}:
            count += 1
    return count


def _scheduler_action_history_entry(selected: Mapping[str, Any], *, tick_index: int) -> dict[str, Any]:
    execution = selected.get("execution_result")
    execution_summary: dict[str, Any] = {}
    if isinstance(execution, Mapping):
        for key in ("ok", "status", "pass", "next_step", "run_id", "task_id", "failure_id"):
            if key in execution:
                execution_summary[key] = execution.get(key)
    return {
        "tick_index": tick_index,
        "action": selected.get("action"),
        "would_wait": selected.get("would_wait"),
        "ok": selected.get("ok"),
        "reason": selected.get("reason"),
        "selected": selected.get("selected"),
        "execution": execution_summary,
    }


def load_scheduler_context(project_root: Path | str, *, workflow_id: str | None = None) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    schema_compatibility = check_project_schema_version(project)
    if schema_compatibility["status"] == "migration_required":
        return {
            "ok": False,
            "message": "Schema migration is required before scheduler startup.",
            "context": None,
            "exit_code": schema_validation_exit_code(schema_compatibility),
            "problem_code": "schema_migration_required",
            "schema_validation": schema_compatibility,
        }
    if schema_compatibility["status"] == "fail":
        return {
            "ok": False,
            "message": "Unable to verify project schema version before scheduler startup.",
            "context": None,
            "exit_code": schema_validation_exit_code(schema_compatibility),
            "problem_code": "schema_validation_failed",
            "schema_validation": schema_compatibility,
        }
    try:
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "ok": False,
            "message": f"Unable to load workflow configuration: {error}",
            "context": None,
        }
    return {
        "ok": True,
        "message": "Workflow configuration loaded.",
        "context": SchedulerContext(
            project=project,
            workflow_id=_workflow_id(workflow_config),
            workflow_config=workflow_config,
            paths=paths,
        ),
    }


def load_scheduler_snapshot(project_root: Path | str) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    context_result = load_scheduler_context(project)
    if not context_result["ok"]:
        problem_code = str(context_result.get("problem_code") or "workflow_config_unavailable")
        problem = {
            "code": problem_code,
            "message": context_result["message"],
        }
        if isinstance(context_result.get("schema_validation"), Mapping):
            problem["schema_validation"] = context_result["schema_validation"]
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "project_root": project.as_posix(),
            "workflow_id": None,
            "load_error": context_result["message"],
            "configuration_problems": [problem],
            "state": {},
            "tasks": [],
            "plan_active": False,
        }

    context: SchedulerContext = context_result["context"]
    paths = context.paths
    state = _read_json_object(paths.runtime_dir / "state.json", default={})
    plan_text = _read_text(paths.plan_file)
    plan_active = "- active: true" in plan_text
    plan_report = inspect_active_plan(paths.plan_file, workflow_id=context.workflow_id) if paths.plan_file.exists() else {}
    plan_sha256 = _sha256_file(paths.plan_file)
    tasks = list(plan_report.get("tasks", [])) if isinstance(plan_report, Mapping) else []
    objectives, objective_parse_errors = parse_plan_objectives(plan_text)
    objective_reports = _objective_report_state(
        paths,
        plan_text=plan_text,
        plan_sha256=plan_sha256,
        objectives=objectives,
    )
    configuration_problems = _configuration_problems(state)
    plan_problem = _active_plan_problem(
        paths=paths,
        state=state,
        plan_active=plan_active,
        plan_report=plan_report,
        plan_sha256=plan_sha256,
    )
    if plan_problem is not None:
        configuration_problems.append(plan_problem)
    if objective_parse_errors:
        configuration_problems.append(
            {
                "code": "plan_objectives_malformed",
                "message": "PLAN.md objective checklist entries are malformed; objective-gated scheduling cannot run safely.",
                "plan_file": paths.value("plan_file"),
                "errors": objective_parse_errors,
            }
        )
    runner_config, runner_problem = _load_runner_config(project)
    if runner_problem:
        configuration_problems.append({"code": "agent_runner_config_unavailable", "message": runner_problem})
    snapshot_now = datetime.now(UTC)
    active_run_leases = _read_active_run_leases(paths, now=snapshot_now)
    background_jobs = _read_background_jobs(
        paths,
        workflow_id=context.workflow_id,
        now=snapshot_now,
    )
    background_jobs.extend(_background_jobs_from_recent_agent_statuses(paths, existing_jobs=background_jobs, now=snapshot_now))

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "project_root": project.as_posix(),
        "workflow_id": context.workflow_id,
        "workflow_config": dict(context.workflow_config),
        "paths": paths,
        "state": state,
        "plan_active": plan_active,
        "plan_report": plan_report,
        "plan_sha256": plan_sha256,
        "tasks": tasks,
        "objectives": [objective.to_dict() for objective in objectives],
        "objective_parse_errors": objective_parse_errors,
        "objective_reports": objective_reports,
        "control_requests": _read_jsonl(paths.runtime_dir / CONTROL_REQUESTS_FILENAME),
        "control_responses": _read_jsonl(paths.runtime_dir / CONTROL_RESPONSES_FILENAME),
        "approval_policy": load_approval_policy(paths),
        "approval_requests": _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME),
        "approval_responses": _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME),
        "active_run_leases": active_run_leases,
        "background_jobs": background_jobs,
        "failure_registry": _read_failure_registry(paths, workflow_id=context.workflow_id),
        "runner_health": _read_runner_health(paths, workflow_id=context.workflow_id),
        "configuration_problems": configuration_problems,
        "runner_config": runner_config,
        "runner_problem": runner_problem,
        "completion_marker": completion_marker_status(paths),
    }


def _active_plan_problem(
    *,
    paths: WorkflowPaths,
    state: Mapping[str, Any],
    plan_active: bool,
    plan_report: Mapping[str, Any],
    plan_sha256: str | None,
) -> dict[str, Any] | None:
    if not plan_active:
        return None
    if plan_report and plan_report.get("valid") is not True:
        return {
            "code": "plan_malformed",
            "message": "Active PLAN.md is malformed; scheduler will not infer task state.",
            "plan_file": paths.value("plan_file"),
            "errors": [str(error) for error in plan_report.get("errors", [])],
        }

    accepted_sha = _accepted_plan_sha256(state)
    manual_change = state.get("manual_plan_change")
    if isinstance(manual_change, Mapping) and manual_change.get("reconciliation_required") is True:
        return {
            "code": "manual_plan_change_detected",
            "message": (
                "PLAN.md changed outside an authorized plan update and requires explicit plan acknowledgement "
                "with `loopplane acknowledge-plan` or an approved plan revision before more work runs."
            ),
            "plan_file": paths.value("plan_file"),
            "accepted_plan_sha256": accepted_sha or manual_change.get("accepted_plan_sha256"),
            "current_plan_sha256": plan_sha256 or manual_change.get("current_plan_sha256"),
            "detected_at": manual_change.get("detected_at"),
            "event_id": manual_change.get("event_id"),
        }
    if accepted_sha and plan_sha256 and accepted_sha != plan_sha256:
        return {
            "code": "manual_plan_change_detected",
            "message": (
                "PLAN.md changed outside an authorized plan update and requires explicit plan acknowledgement "
                "with `loopplane acknowledge-plan` or an approved plan revision before more work runs."
            ),
            "plan_file": paths.value("plan_file"),
            "accepted_plan_sha256": accepted_sha,
            "current_plan_sha256": plan_sha256,
        }
    return None


def _accepted_plan_sha256(state: Mapping[str, Any]) -> str | None:
    value = state.get("active_plan_sha256")
    if isinstance(value, str) and value.strip():
        return value.strip()
    planning = state.get("planning")
    if isinstance(planning, Mapping):
        value = planning.get("active_plan_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip()
    reconciler = state.get("reconciler")
    if isinstance(reconciler, Mapping):
        value = reconciler.get("active_plan_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip()
    change_requests = state.get("change_requests")
    if isinstance(change_requests, Mapping):
        value = change_requests.get("active_plan_sha256")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _record_manual_plan_change_if_needed(snapshot: Mapping[str, Any], *, owner: str) -> None:
    paths = snapshot.get("paths")
    if not isinstance(paths, WorkflowPaths):
        return
    problem = _configuration_problem_by_code(snapshot, "manual_plan_change_detected")
    if problem is None:
        return
    state = snapshot.get("state")
    state_map = state if isinstance(state, Mapping) else {}
    existing = state_map.get("manual_plan_change")
    current_sha = str(problem.get("current_plan_sha256") or "")
    if (
        isinstance(existing, Mapping)
        and existing.get("reconciliation_required") is True
        and str(existing.get("current_plan_sha256") or "") == current_sha
    ):
        return

    workflow_id = str(snapshot.get("workflow_id") or "unknown_workflow")
    event = append_event(
        paths,
        workflow_id=workflow_id,
        event_type="manual_plan_change_detected",
        data={
            "owner": owner,
            "plan_file": problem.get("plan_file"),
            "accepted_plan_sha256": problem.get("accepted_plan_sha256"),
            "current_plan_sha256": problem.get("current_plan_sha256"),
            "message": problem.get("message"),
        },
    )
    state_path = paths.runtime_dir / "state.json"
    loaded = _read_json_object(state_path, default={})
    state_update = dict(loaded) if isinstance(loaded, Mapping) else {}
    state_update["schema_version"] = str(state_update.get("schema_version") or SCHEMA_VERSION)
    state_update["workflow_id"] = str(state_update.get("workflow_id") or workflow_id)
    state_update["status"] = "waiting_config"
    state_update["updated_at"] = utc_timestamp()
    recorded_problem = dict(problem)
    recorded_problem["event_id"] = event.get("event_id")
    state_update["configuration_problems"] = _replace_configuration_problem(
        state_update.get("configuration_problems"),
        recorded_problem,
    )
    state_update["manual_plan_change"] = {
        "reconciliation_required": True,
        "detected_at": event.get("ts") or event.get("timestamp"),
        "event_id": event.get("event_id"),
        "plan_file": problem.get("plan_file"),
        "accepted_plan_sha256": problem.get("accepted_plan_sha256"),
        "current_plan_sha256": problem.get("current_plan_sha256"),
    }
    scheduler = state_update.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
    scheduler.update(
        {
            "last_action": "manual_plan_change_detected",
            "owner": owner,
            "last_plan_change_event_id": event.get("event_id"),
            "heartbeat_at": utc_timestamp(),
        }
    )
    state_update["scheduler"] = scheduler
    _write_json(state_path, state_update)


def _configuration_problem_by_code(snapshot: Mapping[str, Any], code: str) -> dict[str, Any] | None:
    for problem in snapshot.get("configuration_problems", []):
        if isinstance(problem, Mapping) and str(problem.get("code") or "") == code:
            return dict(problem)
    return None


def _replace_configuration_problem(existing: object, replacement: Mapping[str, Any]) -> list[dict[str, Any]]:
    code = str(replacement.get("code") or "configuration_problem")
    problems: list[dict[str, Any]] = []
    if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes)):
        for problem in existing:
            if not isinstance(problem, Mapping):
                continue
            if str(problem.get("code") or "") == code:
                continue
            if str(problem.get("code") or "") == "runtime_waiting_config":
                continue
            problems.append(dict(problem))
    return [dict(replacement), *problems]


def _read_active_run_leases(paths: WorkflowPaths, *, now: datetime) -> list[dict[str, Any]]:
    lease_dir = paths.runtime_dir / "active_run_leases"
    if not lease_dir.exists():
        return []
    leases: list[dict[str, Any]] = []
    for lease_file in sorted(path for path in lease_dir.glob("*.json") if path.is_file()):
        rel = _path_for_record(paths.project_root, lease_file)
        lease = _read_json_object(lease_file, default=None)
        if not isinstance(lease, Mapping):
            leases.append(
                {
                    "path": rel,
                    "status": "needs_recovery",
                    "status_problem": "malformed_lease",
                    "next_prompt_ready": False,
                    "output_inspection": _lease_output_inspection(paths, {}),
                }
            )
            continue
        status = str(lease.get("status") or "running").strip().lower()
        if status in ACTIVE_RUN_LEASE_INACTIVE_STATUSES:
            continue
        heartbeat = _parse_iso_timestamp(lease.get("heartbeat_at"))
        expires = _parse_iso_timestamp(lease.get("lease_expires_at"))
        fresh = (expires is not None and expires >= now) or (
            heartbeat is not None and max(0, int((now - heartbeat).total_seconds())) <= ACTIVE_RUN_LEASE_TTL_SECONDS
        )
        process_liveness = _active_lease_process_liveness(lease)
        alive = process_liveness.get("alive")
        liveness = str(process_liveness.get("liveness") or "unavailable")
        status_problem = None
        normalized_status = status
        reclaimed = False
        if status not in ACTIVE_RUN_LEASE_ACTIVE_STATUSES:
            normalized_status = "needs_recovery"
            status_problem = f"unknown_status:{status}"
        elif not fresh and alive is True:
            status_problem = "stale_heartbeat_process_alive"
        elif not fresh and alive is False:
            # Crash-safe reclaim: an active lease whose runner process is
            # *definitively* dead (not merely unreachable/unavailable) and whose
            # heartbeat has expired cannot still be writing. Releasing it mirrors
            # the scheduler instance-lock's _reclaim_stale_owner and prevents a
            # dead runner (e.g. SIGKILL, OOM, host restart) from wedging the
            # workflow in requires_attention forever. Conservative on purpose: we
            # only reclaim when liveness is "dead", never "unavailable"/unknown,
            # so a runner on another host is never falsely reclaimed.
            normalized_status = "released"
            status_problem = "stale_heartbeat_process_dead_reclaimed"
            reclaimed = True
        elif not fresh:
            normalized_status = "stale"
            status_problem = "stale_heartbeat"
        if reclaimed:
            _release_stale_dead_run_lease(lease_file, lease)
            # Released leases are inactive: drop from the active set so the
            # scheduler can proceed with normal work on this same tick.
            continue
        record = dict(lease)
        record.update(
            {
                "path": rel,
                "status": normalized_status,
                "blocks_scheduler": _lease_blocks_scheduler(lease),
                "fresh": fresh,
                "runner_liveness": liveness,
                "process_liveness": process_liveness,
                "next_prompt_ready": False,
                "output_inspection": _lease_output_inspection(paths, lease),
            }
        )
        if status_problem:
            record["status_problem"] = status_problem
        leases.append(record)
    return leases


def _release_stale_dead_run_lease(lease_file: Path, lease: Mapping[str, Any]) -> None:
    """Persist a 'released' terminal status onto a stale lease whose runner is
    confirmed dead, so the reclaim survives across ticks and is auditable. Best
    effort: a failed write simply leaves the lease for the next tick to retry."""
    try:
        record = dict(lease)
        record["status"] = "released"
        record["status_problem"] = "stale_heartbeat_process_dead_reclaimed"
        record["released_at"] = utc_timestamp()
        record["released_reason"] = "runner_process_dead_and_heartbeat_stale"
        _write_active_run_lease(lease_file, record)
    except OSError:
        pass


def _lease_output_inspection(paths: WorkflowPaths, lease: Mapping[str, Any]) -> dict[str, Any]:
    inspected: dict[str, Any] = {}
    for key in ("adapter_result_path", "stdout_path", "stderr_path", "final_output_path", "agent_status_path"):
        value = lease.get(key)
        if not isinstance(value, str) or not value.strip():
            inspected[key] = {"path": None, "exists": False}
            continue
        path = Path(value)
        if not path.is_absolute():
            path = paths.project_root / value
        inspected[key] = {
            "path": _path_for_record(paths.project_root, path),
            "exists": path.exists(),
        }
    return inspected


def select_next_action(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    from runtime.self_expansion import expansion_candidate, expansion_resolution_candidate

    considered: list[dict[str, str]] = []

    control = _next_control_request(snapshot)
    if control is not None:
        return _action(
            "handle_control_request",
            reason="Next pending control request selected.",
            selected={"request_id": control_record_id(control), "request": control, "run_kind": "control"},
            considered=considered,
        )
    considered.append({"candidate": "control_request", "result": "none"})

    control_wait = _runtime_control_wait(snapshot)
    if control_wait is not None:
        return _action(
            control_wait["action"],
            reason=control_wait["reason"],
            selected=control_wait["selected"],
            would_wait=True,
            blocking_conditions=control_wait["blocking_conditions"],
            considered=considered,
        )
    considered.append({"candidate": "runtime_control_state", "result": "none"})

    approval = _approval_gate(snapshot)
    if approval is not None:
        return _action(
            approval["action"],
            reason=approval["reason"],
            selected=approval["selected"],
            would_wait=True,
            blocking_conditions=approval["blocking_conditions"],
            considered=considered,
        )
    considered.append({"candidate": "approval", "result": "none"})

    active_run_lease = _active_run_lease_not_ready(snapshot)
    if active_run_lease is not None:
        attention_problem = _active_run_lease_attention_problem(active_run_lease)
        if attention_problem is not None:
            return _action(
                "requires_attention",
                reason=attention_problem["reason"],
                selected={
                    "type": "active_run_lease_needs_recovery",
                    "job_id": _record_id(active_run_lease),
                    "job": active_run_lease,
                    "run_kind": "active_run_lease_recovery",
                },
                would_wait=True,
                blocking_conditions=["requires_attention", attention_problem["blocking_condition"]],
                considered=considered,
            )
        return _action(
            "wait_background_job",
            reason="An active run lease is not safe to continue past yet.",
            selected={"job_id": _record_id(active_run_lease), "job": active_run_lease, "run_kind": "active_run_lease_wait"},
            would_wait=True,
            blocking_conditions=["active_run_lease_not_ready"],
            considered=considered,
        )
    considered.append({"candidate": "active_run_lease", "result": "none"})

    background_job = _background_job_not_ready(snapshot)
    if background_job is not None:
        attention_problem = _background_job_attention_problem(background_job)
        if attention_problem is not None:
            delegated_failure = _failure_for_background_job(snapshot, background_job)
            if delegated_failure is None:
                return _action(
                    "requires_attention",
                    reason=attention_problem["reason"],
                    selected={
                        "type": "background_job_needs_recovery",
                        "job_id": _record_id(background_job),
                        "task_id": background_job.get("task_id"),
                        "run_id": background_job.get("run_id"),
                        "job": background_job,
                        "run_kind": "background_job_recovery",
                        "message": attention_problem["reason"],
                    },
                    would_wait=True,
                    blocking_conditions=["requires_attention", attention_problem["blocking_condition"]],
                    considered=considered,
                )
            considered.append(
                {
                    "candidate": "background_job",
                    "result": (
                        "delegated_to_autonomous_recovery:"
                        + str(delegated_failure.get("status") or "unrecovered")
                    ),
                }
            )
        else:
            return _action(
                "wait_background_job",
                reason="An active background job is not safe to continue past yet.",
                selected={"job_id": _record_id(background_job), "job": background_job, "run_kind": "background_wait"},
                would_wait=True,
                blocking_conditions=["background_job_not_ready"],
                considered=considered,
            )
    else:
        considered.append({"candidate": "background_job", "result": "none"})

    config_problem = _config_wait(snapshot)
    if config_problem is not None:
        return _action(
            "wait_config",
            reason=config_problem["message"],
            selected={"problem": config_problem, "run_kind": "config_wait"},
            would_wait=True,
            blocking_conditions=["waiting_config"],
            considered=considered,
        )
    considered.append({"candidate": "config_wait", "result": "none"})

    failure = _oldest_recoverable_failure(snapshot)
    if failure is not None:
        runner = _runner_for_role(snapshot, "recovery_worker") or _runner_for_role(snapshot, "worker")
        if runner is None:
            return _runner_wait_or_config_action(
                snapshot,
                roles=("recovery_worker", "worker"),
                config_reason="No enabled recovery worker or worker runner is configured.",
                config_code="recovery_runner_unavailable",
                considered=considered,
            )
        return _action(
            "run_recovery",
            reason="Oldest recoverable failure within budget selected before new work.",
            selected={
                "role": "recovery_worker",
                "runner_id": runner.runner_id,
                "runner_role": runner.role,
                "task_id": failure.get("task_id"),
                "failure_id": _record_id(failure),
                "run_kind": "recovery",
            },
            considered=considered,
        )
    considered.append({"candidate": "recoverable_failure", "result": "none"})

    resolution = expansion_resolution_candidate(snapshot)
    if resolution is not None:
        return _action(
            "resolve_expansion_failure",
            reason="Self-expansion evidence tasks completed; target failure can be reopened for scoped recovery.",
            selected=resolution,
            considered=considered,
        )
    considered.append({"candidate": "self_expansion_resolution", "result": "none"})

    recovery_expansion = expansion_candidate(snapshot, mode="no_executable")
    if (
        recovery_expansion is not None
        and str(recovery_expansion.get("trigger") or "") == "recovery_exhausted"
        and _next_executable_task(snapshot) is not None
    ):
        expansion_action = _self_expansion_selected_action(
            snapshot,
            recovery_expansion,
            reason="Recovery budget is exhausted for an unresolved failure; self-expansion planner selected before later work.",
            considered=considered,
        )
        if expansion_action is not None:
            return expansion_action
    considered.append({"candidate": "self_expansion_recovery_exhausted", "result": "none"})

    phase_objective_gate = _phase_objective_gate_candidate(snapshot)
    if phase_objective_gate is not None:
        gate_action = _objective_gate_scheduler_action(
            snapshot,
            phase_objective_gate,
            considered=considered,
            verification_reason="Current phase tasks are terminal; agentic phase objective verification selected before later work.",
            expansion_reason="Current phase objective verification found expandable gaps; self-expansion planner selected.",
        )
        if gate_action is not None:
            return gate_action
    considered.append({"candidate": "phase_objective_gate", "result": "none"})

    task = _next_executable_task(snapshot)
    if task is not None:
        runner = _runner_for_role(snapshot, "worker")
        if runner is None:
            return _runner_wait_or_config_action(
                snapshot,
                roles=("worker",),
                config_reason="No enabled worker runner is configured.",
                config_code="worker_runner_unavailable",
                considered=considered,
            )
        return _action(
            "run_worker",
            reason="Earliest executable pending task selected.",
            selected={
                "role": "worker",
                "runner_id": runner.runner_id,
                "task_id": task["task_id"],
                "run_kind": "normal",
            },
            considered=considered,
        )
    considered.append({"candidate": "worker_task", "result": "none"})

    completion_marker = snapshot.get("completion_marker")
    if isinstance(completion_marker, Mapping) and completion_marker.get("fresh") is True:
        return _action(
            "complete",
            reason="Fresh completion marker matches current runtime state.",
            selected={"run_kind": "completion", "marker_path": completion_marker.get("path")},
            would_wait=True,
            considered=considered,
        )
    considered.append({"candidate": "completion_marker", "result": "missing_or_stale"})

    final_expansion = expansion_candidate(snapshot, mode="final_failure")
    if final_expansion is not None:
        expansion_action = _self_expansion_selected_action(
            snapshot,
            final_expansion,
            reason="Final verification failed with expandable blockers; self-expansion planner selected.",
            considered=considered,
        )
        if expansion_action is not None:
            return expansion_action
    considered.append({"candidate": "self_expansion_final_failure", "result": "none"})

    final_objective_gate = _final_objective_gate_candidate(snapshot)
    if final_objective_gate is not None:
        gate_action = _objective_gate_scheduler_action(
            snapshot,
            final_objective_gate,
            considered=considered,
            verification_reason="All tasks are terminal; agentic final objective verification selected before final completion gates.",
            expansion_reason="Final objective verification found expandable gaps; self-expansion planner selected.",
        )
        if gate_action is not None:
            return gate_action
    considered.append({"candidate": "final_objective_gate", "result": "none"})

    final = _final_verification_candidate(snapshot)
    if final is not None:
        return _action(
            "run_final_verification",
            reason="No executable task remains; final verification gates selected.",
            selected=final,
            considered=considered,
        )

    idle_expansion = expansion_candidate(snapshot, mode="no_executable")
    if idle_expansion is not None:
        expansion_action = _self_expansion_selected_action(
            snapshot,
            idle_expansion,
            reason="No executable task remains, but self-expansion can add follow-up work.",
            considered=considered,
        )
        if expansion_action is not None:
            return expansion_action
    considered.append({"candidate": "self_expansion_no_executable", "result": "none"})

    return _action(
        "wait_no_executable_work",
        reason="No executable task, recoverable failure, or final verification candidate is available.",
        selected={"run_kind": "idle"},
        would_wait=True,
        blocking_conditions=["no_executable_work"],
        considered=considered,
    )


def execute_selected_action(
    snapshot: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    owner: str,
    lease_heartbeat_interval_seconds: float = ACTIVE_RUN_LEASE_HEARTBEAT_INTERVAL_SECONDS,
    emit_wait_event: bool = True,
) -> dict[str, Any] | None:
    paths = snapshot.get("paths")
    if not isinstance(paths, WorkflowPaths):
        return None
    workflow_id = str(snapshot.get("workflow_id") or "unknown_workflow")
    action_name = str(action.get("action"))

    if action_name == "handle_control_request":
        request = dict(action.get("selected", {}).get("request", {}))
        response = _handle_control_request(paths, snapshot, request, owner=owner)
        append_event(
            paths,
            workflow_id=workflow_id,
            event_type="control_request_handled",
            data={"owner": owner, "request_id": response.get("request_id"), "action": response.get("action")},
        )
        return response

    if action_name in {
        "wait_paused",
        "wait_stopped",
        "wait_approval",
        "requires_attention",
        "wait_background_job",
        "wait_config",
        "wait_no_executable_work",
        "wait_runner_availability",
    }:
        status = _scheduler_wait_status_for_action(action_name)
        scheduler_update: dict[str, Any] = {"last_action": action_name, "owner": owner}
        attention_items: list[dict[str, Any]] | None = None
        if action_name == "wait_approval":
            selected = action.get("selected", {}) if isinstance(action.get("selected"), Mapping) else {}
            if selected.get("approval_request_needed"):
                task = selected.get("task") if isinstance(selected.get("task"), Mapping) else None
                if task is not None:
                    approval_request = _record_task_approval_request(paths, workflow_id=workflow_id, task=task)
                    scheduler_update["last_approval_id"] = approval_request.get("approval_id")
                    selected = {**dict(selected), "approval_id": approval_request.get("approval_id"), "approval": approval_request}
                    action["selected"] = selected
                    append_event(
                        paths,
                        workflow_id=workflow_id,
                        event_type="approval_requested",
                        data={
                            "owner": owner,
                            "approval_id": approval_request.get("approval_id"),
                            "task_id": approval_request.get("task_id"),
                            "type": approval_request.get("type"),
                        },
                    )
        if action_name == "requires_attention":
            selected = action.get("selected", {}) if isinstance(action.get("selected"), Mapping) else {}
            if str(selected.get("type") or "") == "background_job_needs_recovery":
                _persist_background_jobs_from_snapshot(paths, workflow_id, snapshot)
                selected_job = selected.get("job")
                if isinstance(selected_job, Mapping):
                    scheduler_update["active_background_job_id"] = _record_id(selected_job)
                    scheduler_update["active_background_job_status"] = selected_job.get("status")
                    scheduler_update["wake_next_agent_when"] = selected_job.get("wake_next_agent_when")
            attention_item = _requires_attention_item(selected, reason=str(action.get("reason") or "Requires attention."))
            attention_items = [attention_item]
            scheduler_update["requires_attention_id"] = attention_item.get("request_id")
            scheduler_update["requires_attention_type"] = attention_item.get("type")
            if str(selected.get("type") or "") == "objective_unresolved":
                status = "objective_unresolved"
                objective_ids = [str(item) for item in selected.get("target_objective_ids", []) if str(item)]
                objective_gate = selected.get("objective_gate") if isinstance(selected.get("objective_gate"), Mapping) else {}
                if not objective_ids and isinstance(objective_gate, Mapping):
                    objective_ids = [str(item) for item in objective_gate.get("unresolved_objective_ids", []) if str(item)]
                scheduler_update["objective_unresolved_ids"] = objective_ids
                try:
                    from runtime.objective_verification import mark_objectives_unresolved

                    objective_update = mark_objectives_unresolved(
                        paths.project_root,
                        objective_ids,
                        reason=str(action.get("reason") or "Objective gate unresolved."),
                        owner=owner,
                    )
                    scheduler_update["objective_marker_update"] = {
                        "status": objective_update.get("status"),
                        "mutated_plan": objective_update.get("mutated_plan"),
                        "status_updates": objective_update.get("status_updates"),
                    }
                except Exception as error:
                    scheduler_update["objective_marker_update"] = {"status": "error", "error": str(error)}
                try:
                    registry_update = mark_workflow_objective_unresolved(
                        paths.project_root,
                        workflow_id,
                        target_objective_ids=objective_ids,
                        summary={
                            "reason": str(action.get("reason") or "Objective gate unresolved."),
                            "objective_gate": _json_safe_object(objective_gate),
                        },
                        updated_by=owner,
                    )
                    scheduler_update["workflow_registry_update"] = {
                        "status": registry_update.get("status"),
                        "workflow_id": registry_update.get("workflow_id"),
                    }
                except Exception as error:
                    scheduler_update["workflow_registry_update"] = {"status": "error", "error": str(error)}
        if action_name == "wait_background_job":
            _persist_background_jobs_from_snapshot(paths, workflow_id, snapshot)
            selected_job = action.get("selected", {}).get("job") if isinstance(action.get("selected"), Mapping) else None
            if isinstance(selected_job, Mapping):
                scheduler_update["active_background_job_id"] = _record_id(selected_job)
                scheduler_update["active_background_job_status"] = selected_job.get("status")
                scheduler_update["wake_next_agent_when"] = selected_job.get("wake_next_agent_when")
        _update_runtime_state(paths, status=status, scheduler_update=scheduler_update, requires_attention=attention_items)
        if emit_wait_event:
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_requires_attention" if action_name == "requires_attention" else "scheduler_waiting",
                data={
                    "owner": owner,
                    "status": status,
                    "reason": action.get("reason"),
                    "selected": _json_safe_object(action.get("selected", {})),
                },
            )
        return None

    if action_name == "complete":
        _update_runtime_state(
            paths,
            status="completed",
            scheduler_update={"last_action": action_name, "owner": owner, "running": False, "paused": False, "stop_requested": False},
        )
        marker = snapshot.get("completion_marker") if isinstance(snapshot.get("completion_marker"), Mapping) else {}
        marker_path = _completion_marker_record_path(paths, marker)
        if _workflow_registry_completion_is_current(paths, workflow_id, marker_path):
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "status": "completed",
                "workflow_id": workflow_id,
                "completion_marker": marker_path,
                "workflow_registry_update": {"status": "current", "mutated": False},
                "message": "Workflow is already completed; registry completion metadata is current.",
            }
        return mark_workflow_completed(
            paths.project_root,
            workflow_id,
            completion_marker=marker_path,
            summary=_scheduler_completion_summary(snapshot),
            updated_by=owner,
        )

    if action_name == "run_worker":
        result = _execute_worker_action(
            snapshot,
            action,
            owner=owner,
            lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
            prepared_role="worker",
            scheduler_action="run_worker",
        )
        return _auto_validate_and_reconcile_after_worker(result)

    if action_name == "run_recovery":
        result = _execute_worker_action(
            snapshot,
            action,
            owner=owner,
            lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
            prepared_role="recovery_worker",
            scheduler_action="run_recovery",
        )
        return _auto_validate_and_reconcile_after_worker(result)

    if action_name == "resolve_expansion_failure":
        from runtime.self_expansion import reopen_expansion_failures

        selected = action.get("selected", {}) if isinstance(action.get("selected"), Mapping) else {}
        result = reopen_expansion_failures(
            paths.project_root,
            proposal_id=str(selected.get("proposal_id") or ""),
            target_failure_ids=[str(item) for item in selected.get("target_failure_ids", []) if str(item)],
            added_task_ids=[str(item) for item in selected.get("added_task_ids", []) if str(item)],
            owner=owner,
        )
        _update_runtime_state(
            paths,
            status="self_expansion_failure_reopened" if result.get("ok") else "self_expansion_resolution_failed",
            scheduler_update={"last_action": action_name, "owner": owner, "selected": selected, "last_self_expansion_result": result.get("status")},
        )
        return result

    if action_name == "run_expansion_planner":
        return _execute_expansion_action(
            snapshot,
            action,
            owner=owner,
            lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
        )

    if action_name in {"run_phase_objective_verifier", "run_final_objective_verifier"}:
        return _execute_objective_verifier_action(
            snapshot,
            action,
            owner=owner,
            lease_heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
        )

    if action_name == "run_final_verification":
        result = _run_final_verification(paths, snapshot, owner=owner)
        return result

    return None


def _self_expansion_selected_action(
    snapshot: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    reason: str,
    considered: Sequence[Mapping[str, str]],
) -> dict[str, Any] | None:
    trigger = str(candidate.get("trigger") or "")
    if trigger == "self_expansion_budget_exhausted":
        return _action(
            "requires_attention",
            reason="Self-expansion budget is exhausted.",
            selected={
                "type": "self_expansion_budget_exhausted",
                "run_kind": "self_expansion_budget",
                "message": "Self-expansion policy budget is exhausted.",
                "problem": candidate.get("problem"),
            },
            would_wait=True,
            blocking_conditions=["requires_attention", "self_expansion_budget_exhausted"],
            considered=considered,
        )
    runner = _runner_for_role(snapshot, "expansion_planner")
    if runner is None:
        return _runner_wait_or_config_action(
            snapshot,
            roles=("expansion_planner",),
            config_reason="No enabled self-expansion planner runner is configured.",
            config_code="expansion_planner_runner_unavailable",
            considered=considered,
        )
    return _action(
        "run_expansion_planner",
        reason=reason,
        selected={
            "role": "expansion_planner",
            "runner_id": runner.runner_id,
            "runner_role": runner.role,
            "run_kind": "self_expansion",
            "candidate": dict(candidate),
        },
        considered=considered,
    )


def _objective_gate_scheduler_action(
    snapshot: Mapping[str, Any],
    gate: Mapping[str, Any],
    *,
    considered: Sequence[Mapping[str, str]],
    verification_reason: str,
    expansion_reason: str,
) -> dict[str, Any] | None:
    status = str(gate.get("objective_gate_status") or gate.get("status") or "")
    if status == "needs_verification":
        runner = _runner_for_role(snapshot, "objective_verifier")
        if runner is None:
            return _runner_wait_or_config_action(
                snapshot,
                roles=("objective_verifier",),
                config_reason="No enabled objective verifier runner is configured.",
                config_code="objective_verifier_runner_unavailable",
                considered=considered,
            )
        action_name = str(gate.get("action_name") or "run_phase_objective_verifier")
        selected = dict(gate)
        selected["runner_id"] = runner.runner_id
        selected["runner_role"] = runner.role
        return _action(action_name, reason=verification_reason, selected=selected, considered=considered)
    if status == "needs_expansion":
        from runtime.self_expansion import expansion_candidate

        candidate = expansion_candidate(snapshot, mode="objective_gap")
        if candidate is not None:
            expansion_action = _self_expansion_selected_action(
                snapshot,
                candidate,
                reason=expansion_reason,
                considered=considered,
            )
            if expansion_action is not None:
                return expansion_action
        return _action(
            "requires_attention",
            reason="Objective verification found a gap, but no self-expansion candidate is currently available.",
            selected={
                "type": "objective_gap_no_expansion_candidate",
                "run_kind": "objective_unresolved",
                "objective_gate": dict(gate),
            },
            would_wait=True,
            blocking_conditions=["requires_attention", "objective_gap"],
            considered=considered,
        )
    if status == "unresolved":
        return _action(
            "requires_attention",
            reason="Objective verification found non-expandable unresolved objectives.",
            selected={
                "type": "objective_unresolved",
                "run_kind": "objective_unresolved",
                "target_objective_ids": list(gate.get("unresolved_objective_ids") or gate.get("target_objective_ids") or []),
                "expansion_exhausted_objective_ids": list(gate.get("expansion_exhausted_objective_ids") or []),
                "objective_gate": dict(gate),
            },
            would_wait=True,
            blocking_conditions=["requires_attention", "objective_unresolved"],
            considered=considered,
        )
    return None


def _runner_wait_or_config_action(
    snapshot: Mapping[str, Any],
    *,
    roles: Sequence[str],
    config_reason: str,
    config_code: str,
    considered: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    hold = _active_hold_for_roles(snapshot, roles)
    if hold is not None:
        reason_class = str(hold.get("reason_class") or "runner_unavailable")
        cooldown_until = str(hold.get("cooldown_until") or "")
        reason = f"Runner availability hold is active: {reason_class}."
        if cooldown_until:
            reason = f"{reason} Cooldown until {cooldown_until}."
        if hold.get("requires_attention") is True:
            return _action(
                "requires_attention",
                reason=reason,
                selected={
                    "type": "runner_availability_requires_attention",
                    "run_kind": "runner_availability_wait",
                    "roles": [str(role) for role in roles],
                    "hold": _json_safe_object(hold),
                },
                would_wait=True,
                blocking_conditions=["requires_attention", "runner_availability"],
                considered=considered,
            )
        return _action(
            "wait_runner_availability",
            reason=reason,
            selected={
                "run_kind": "runner_availability_wait",
                "roles": [str(role) for role in roles],
                "hold": _json_safe_object(hold),
            },
            would_wait=True,
            blocking_conditions=["runner_availability"],
            considered=considered,
        )
    return _action(
        "wait_config",
        reason=config_reason,
        selected={"problem": {"code": config_code}, "run_kind": "config_wait"},
        would_wait=True,
        blocking_conditions=["waiting_config"],
        considered=considered,
    )


def _auto_validate_and_reconcile_after_worker(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or str(result.get("next_step") or "") != "validation_pending":
        return result
    task_id = str(result.get("task_id") or "").strip()
    run_dir = str(result.get("role_output_dir") or "").strip()
    if not task_id or not run_dir:
        return result

    from runtime.validation import PASSING_STATUSES, run_validator

    project_root = Path(str(result.get("project_root") or ".")).expanduser().resolve()
    validation = run_validator(project_root, task_id=task_id, run_dir=run_dir, write=True)
    result["auto_validation"] = validation
    validation_status = str(validation.get("status") or "")
    if validation_status in PASSING_STATUSES:
        result["auto_validation_checkpoint"] = _maybe_create_worker_policy_checkpoint(
            project_root,
            reason="after_validation_pass",
            task_id=task_id,
            run_id=str(result.get("run_id") or ""),
        )
    from runtime.reconciliation import run_reconciler

    reconciliation = run_reconciler(project_root, task_id=task_id, run_dir=run_dir, write=True)
    result["auto_reconciliation"] = reconciliation
    reconciliation_status = str(reconciliation.get("status") or "")
    if validation_status in PASSING_STATUSES and reconciliation_status == "reconciled":
        result["next_step"] = "reconciled"
        result["message"] = f"{result.get('message', 'Worker completed')} Validation and reconciliation completed."
        return result
    if str(result.get("role") or "") == "recovery_worker" and validation_status in PASSING_STATUSES:
        result["ok"] = True
        result["status"] = "completed"
        result["next_step"] = "recovery_validated"
        result["message"] = f"{result.get('message', 'Recovery worker completed')} Validation completed; reconciliation did not close the task."
        return result
    result["ok"] = False
    result["next_step"] = "needs_human" if validation_status == "needs_human" else "recovery_pending"
    result["status"] = "failed_validation" if validation_status != "needs_human" else "needs_human"
    return result


def append_event(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    event_type: str,
    data: Mapping[str, Any],
    run_id: str | None = None,
    snapshot_interval: int | None = EVENT_SNAPSHOT_INTERVAL,
) -> dict[str, Any]:
    owner = f"event-append:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "event_append_lock", owner, ttl_seconds=30)
    with lock.acquire():
        return _append_event_unlocked(
            paths,
            workflow_id=workflow_id,
            event_type=event_type,
            data=data,
            run_id=run_id,
            snapshot_interval=snapshot_interval,
        )


def write_event_snapshot(
    paths: WorkflowPaths,
    *,
    workflow_id: str | None = None,
    through_sequence: int | None = None,
) -> Path:
    owner = f"event-snapshot:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "event_append_lock", owner, ttl_seconds=30)
    with lock.acquire():
        return _write_event_snapshot_unlocked(paths, workflow_id=workflow_id, through_sequence=through_sequence)


def load_latest_event_snapshot(paths: WorkflowPaths) -> dict[str, Any]:
    snapshots_dir = paths.runtime_dir / "snapshots"
    latest: dict[str, Any] | None = None
    latest_path: Path | None = None
    latest_sequence = -1
    for path in sorted(snapshots_dir.glob("snapshot_*.json")):
        data = _read_json_object(path, default={})
        if not isinstance(data, Mapping):
            continue
        sequence = _snapshot_sequence(data)
        if sequence > latest_sequence:
            latest = dict(data)
            latest_path = path
            latest_sequence = sequence
    if latest is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_id": "snapshot_000000",
            "events_through_sequence": 0,
            "event_log_head": None,
            "state": _empty_event_projection(None),
            "path": None,
        }
    latest["path"] = latest_path.as_posix() if latest_path is not None else None
    return latest


def replay_events_after_snapshot(
    paths: WorkflowPaths,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved_snapshot = snapshot if snapshot is not None else load_latest_event_snapshot(paths)
    after_sequence = _snapshot_sequence(resolved_snapshot)
    return [
        record
        for record in _iter_event_records(paths.runtime_dir / "events")
        if (_event_sequence_value(record) or 0) > after_sequence
    ]


def load_event_log_projection(paths: WorkflowPaths) -> dict[str, Any]:
    snapshot = load_latest_event_snapshot(paths)
    projection = _projection_from_snapshot(snapshot)
    replayed = replay_events_after_snapshot(paths, snapshot=snapshot)
    for record in replayed:
        _apply_event_to_projection(projection, record)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot": snapshot,
        "events_replayed": len(replayed),
        "state": projection,
    }


def scheduler_exit_code(result: Mapping[str, Any]) -> int:
    selected = result.get("selected_action")
    if isinstance(selected, Mapping):
        derived = _scheduler_exit_code_for_selected(selected, ok=bool(result.get("ok")))
        if derived != EXIT_SUCCESS or result.get("exit_code") in {None, 0, 1}:
            return derived
    try:
        return int(result.get("exit_code", EXIT_GENERIC_FAILURE))
    except (TypeError, ValueError):
        return EXIT_GENERIC_FAILURE


def _scheduler_exit_code_for_selected(selected: Mapping[str, Any] | None, *, ok: bool) -> int:
    if not isinstance(selected, Mapping):
        return EXIT_SUCCESS if ok else EXIT_GENERIC_FAILURE
    action = str(selected.get("action") or "")
    if action == "wait_approval":
        return EXIT_WAITING_APPROVAL
    if action == "wait_background_job":
        return EXIT_WAITING_BACKGROUND_JOB
    if action == "wait_config":
        return _wait_config_exit_code(selected)
    if action == "wait_runner_availability":
        return EXIT_RUNNER_UNAVAILABLE
    if action == "requires_attention":
        return _requires_attention_exit_code(selected)

    execution_result = selected.get("execution_result")
    if isinstance(execution_result, Mapping):
        execution_code = _execution_result_exit_code(action, execution_result)
        if execution_code != EXIT_SUCCESS:
            return execution_code

    return EXIT_SUCCESS if ok else EXIT_GENERIC_FAILURE


def _wait_config_exit_code(selected: Mapping[str, Any]) -> int:
    selected_payload = selected.get("selected")
    problem = selected_payload.get("problem") if isinstance(selected_payload, Mapping) else None
    problem_code = str(problem.get("code") or "") if isinstance(problem, Mapping) else ""
    if "runner_unavailable" in problem_code:
        return EXIT_RUNNER_UNAVAILABLE
    if "migration" in problem_code:
        return EXIT_MIGRATION_REQUIRED
    if problem_code in {"active_plan_missing", "plan_missing", "plan_malformed"}:
        return EXIT_PLAN_MALFORMED
    return EXIT_INVALID_CONFIG


def _requires_attention_exit_code(selected: Mapping[str, Any]) -> int:
    selected_payload = selected.get("selected")
    attention_type = str(selected_payload.get("type") or selected_payload.get("run_kind") or "") if isinstance(selected_payload, Mapping) else ""
    blocking_conditions = {
        str(condition)
        for condition in selected.get("blocking_conditions", [])
        if str(condition)
    }
    if attention_type in {"approval_disabled", "change_request_approval_disabled"} or "approval_required" in blocking_conditions:
        return EXIT_SECURITY_POLICY_VIOLATION
    if "exhausted" in attention_type or "failure_budget_exhausted" in blocking_conditions:
        return EXIT_FAILURE_BUDGET_EXHAUSTED
    return EXIT_GENERIC_FAILURE


def _execution_result_exit_code(action: str, result: Mapping[str, Any]) -> int:
    if action == "handle_control_request":
        return EXIT_SUCCESS if str(result.get("status") or "") == "applied" else EXIT_GENERIC_FAILURE
    if action == "run_final_verification" and not (result.get("status") == "pass" or result.get("pass") is True):
        return _final_verification_result_exit_code(result)

    failure_update = result.get("failure_registry_update")
    if isinstance(failure_update, Mapping) and str(failure_update.get("status") or "") == "exhausted":
        return EXIT_FAILURE_BUDGET_EXHAUSTED
    if str(result.get("status") or "") == "running_background" or str(result.get("next_step") or "") == "waiting_background":
        return EXIT_WAITING_BACKGROUND_JOB
    if str(result.get("status") or "") == "waiting_config" or "runner_unavailable" in str(result.get("error_code") or ""):
        return EXIT_RUNNER_UNAVAILABLE
    if isinstance(result.get("runner_availability"), Mapping):
        return EXIT_RUNNER_UNAVAILABLE
    auto_validation = result.get("auto_validation")
    if isinstance(auto_validation, Mapping):
        auto_status = str(auto_validation.get("status") or "")
        if auto_status == "needs_human":
            return EXIT_NEEDS_HUMAN
        if auto_status not in {"pass", "pass_with_warnings"}:
            return EXIT_VALIDATION_FAILED
    auto_reconciliation = result.get("auto_reconciliation")
    if isinstance(auto_reconciliation, Mapping):
        auto_reconciliation_status = str(auto_reconciliation.get("status") or "")
        if auto_reconciliation_status == "needs_human":
            return EXIT_WAITING_APPROVAL
        if auto_reconciliation_status == "validation_failed":
            return EXIT_VALIDATION_FAILED
    try:
        adapter_exit_code = int(result.get("adapter_exit_code"))
    except (TypeError, ValueError):
        adapter_exit_code = None
    if adapter_exit_code == ADAPTER_POLICY_BLOCKED_EXIT_CODE:
        return EXIT_SECURITY_POLICY_VIOLATION
    if adapter_exit_code == ADAPTER_COMMAND_UNAVAILABLE_EXIT_CODE:
        return EXIT_RUNNER_UNAVAILABLE
    if str(result.get("status") or "") in {"validation_candidate_failed", "failed_validation"}:
        return EXIT_VALIDATION_FAILED
    return EXIT_SUCCESS if result.get("ok") else EXIT_GENERIC_FAILURE


def _final_verification_result_exit_code(result: Mapping[str, Any]) -> int:
    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        failed_checks = {
            str(check.get("check") or "")
            for check in checks
            if isinstance(check, Mapping) and check.get("status") == "fail"
        }
        if "workflow_configuration" in failed_checks:
            return EXIT_INVALID_CONFIG
        if "plan_parseable" in failed_checks:
            return EXIT_PLAN_MALFORMED
        if "no_unrecovered_failures" in failed_checks and has_text(
            result,
            ("exhausted", "failure budget"),
            "blockers",
            "checks",
        ):
            return EXIT_FAILURE_BUDGET_EXHAUSTED
    return EXIT_FINAL_VERIFICATION_FAILED


def preview_scheduler(project_root: Path | str, *, write: bool = False) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    snapshot = load_scheduler_snapshot(project)
    selected_action = select_next_action(snapshot)
    generated_at = utc_timestamp()
    paths = snapshot.get("paths")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(snapshot.get("ok", False)),
        "status": "ok" if snapshot.get("ok", False) else "configuration_unavailable",
        "project_root": project.as_posix(),
        "workflow_id": snapshot.get("workflow_id"),
        "generated_at": generated_at,
        "mode": "dry_run",
        "would_mutate_state": False,
        "next_action": selected_action["action"],
        "reason": selected_action["reason"],
        "would_wait": selected_action["would_wait"],
        "selected": _preview_selected(snapshot, selected_action),
        "completion_marker": snapshot.get("completion_marker", _missing_completion_marker()),
        "blocking_conditions": list(selected_action.get("blocking_conditions", [])),
        "skipped_candidates": _preview_skipped_candidates(selected_action.get("considered", [])),
        "scheduler_selection": selected_action,
    }
    if not snapshot.get("ok", False) and snapshot.get("load_error"):
        result["load_error"] = snapshot.get("load_error")
    if write:
        if isinstance(paths, WorkflowPaths):
            preview_path = paths.runtime_dir / "preview_result.json"
            result["preview_result_path"] = preview_path.as_posix()
            result["preview_result_authoritative"] = False
            _write_json(preview_path, result)
        else:
            result["ok"] = False
            result["status"] = "failed"
            result["write_error"] = "Unable to resolve runtime_dir for preview_result.json."
    return result


def preview_exit_code(result: Mapping[str, Any]) -> int:
    action = {
        "action": result.get("next_action"),
        "selected": result.get("selected"),
        "blocking_conditions": result.get("blocking_conditions", []),
    }
    derived = _scheduler_exit_code_for_selected(action, ok=bool(result.get("ok")))
    if derived != EXIT_SUCCESS:
        return derived
    return EXIT_SUCCESS if result.get("ok") else EXIT_INVALID_CONFIG


def format_preview_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane preview: {result.get('next_action', 'unknown')}",
        str(result.get("reason", "")),
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        "mode: dry_run",
        f"would_mutate_state: {str(result.get('would_mutate_state')).lower()}",
        f"would_wait: {str(result.get('would_wait')).lower()}",
    ]
    selected = result.get("selected")
    if isinstance(selected, Mapping) and selected:
        lines.append("selected:")
        for key in (
            "role",
            "runner_id",
            "task_id",
            "failure_id",
            "approval_id",
            "type",
            "status",
            "run_kind",
            "expected_prompt_path",
            "blocking_reason",
            "message",
        ):
            if selected.get(key) is not None:
                lines.append(f"  {key}: {selected[key]}")
    marker = result.get("completion_marker")
    if isinstance(marker, Mapping):
        if marker.get("exists"):
            marker_state = "fresh" if marker.get("fresh") else "stale"
            lines.append(f"completion_marker: {marker_state}")
            if marker.get("path"):
                lines.append(f"completion_marker_path: {marker['path']}")
            stale_reasons = marker.get("stale_reasons") or []
            if stale_reasons:
                lines.append("completion_marker_stale_reasons:")
                for reason in stale_reasons:
                    lines.append(f"  - {reason}")
        else:
            lines.append("completion_marker: absent")
    blocking = result.get("blocking_conditions") or []
    if blocking:
        lines.append("blocking_conditions:")
        for condition in blocking:
            lines.append(f"  - {condition}")
    skipped = result.get("skipped_candidates") or []
    if skipped:
        lines.append("skipped_candidates:")
        for item in skipped:
            if isinstance(item, Mapping):
                lines.append(f"  - {item.get('candidate')}: {item.get('reason')}")
    return "\n".join(line for line in lines if line) + "\n"


def format_scheduler_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane scheduler: {result.get('status', 'unknown')}",
        str(result.get("message", "")),
    ]
    if result.get("stopped_reason"):
        lines.append(f"stopped_reason: {result.get('stopped_reason')}")
    if result.get("pending_tasks") is not None:
        lines.append(f"pending_tasks: {result.get('pending_tasks')}")
    if result.get("hint"):
        lines.append(f"hint: {result.get('hint')}")
    selected = result.get("selected_action")
    if isinstance(selected, Mapping):
        lines.append(f"action: {selected.get('action')}")
        action_selected = selected.get("selected")
        if isinstance(action_selected, Mapping):
            for key in ("task_id", "runner_id", "role", "run_kind", "failure_id", "request_id", "approval_id", "type", "status"):
                if action_selected.get(key) is not None:
                    lines.append(f"{key}: {action_selected[key]}")
        execution_result = selected.get("execution_result")
        if isinstance(execution_result, Mapping):
            for key in (
                "run_id",
                "run_dir",
                "task_evidence_run_dir",
                "scheduler_run_dir",
                "role_output_dir",
                "agent_status_path",
                "adapter_result_path",
            ):
                if execution_result.get(key) is not None:
                    lines.append(f"{key}: {execution_result[key]}")
    return "\n".join(line for line in lines if line) + "\n"


def scheduler_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    selected = result.get("selected_action")
    selected_action = selected if isinstance(selected, Mapping) else {}
    selected_payload = selected_action.get("selected")
    selected_payload = selected_payload if isinstance(selected_payload, Mapping) else {}
    execution = selected_action.get("execution_result")
    execution = execution if isinstance(execution, Mapping) else {}
    summary = {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "workflow_id": result.get("workflow_id"),
        "action": selected_action.get("action"),
        "would_wait": bool(selected_action.get("would_wait")),
        "stopped_reason": result.get("stopped_reason"),
        "pending_tasks": result.get("pending_tasks"),
        "task_id": selected_payload.get("task_id") or execution.get("task_id"),
        "runner_id": selected_payload.get("runner_id") or execution.get("runner_id"),
        "role": selected_payload.get("role") or execution.get("role"),
        "run_id": execution.get("run_id"),
        "next_step": execution.get("next_step"),
        "exit_code": result.get("exit_code"),
        "message": result.get("message"),
    }
    compact = {key: value for key, value in summary.items() if value is not None}
    hint = result.get("hint")
    if hint:
        compact["hint"] = hint
    return compact


def format_scheduler_summary_text(result: Mapping[str, Any]) -> str:
    summary = scheduler_summary(result)
    fields = [
        ("status", summary.get("status")),
        ("workflow", summary.get("workflow_id")),
        ("action", summary.get("action")),
        ("task", summary.get("task_id") or "none"),
        ("run", summary.get("run_id") or "none"),
        ("next", summary.get("next_step") or summary.get("stopped_reason") or "none"),
        ("pending", summary.get("pending_tasks")),
        ("exit", summary.get("exit_code")),
    ]
    first = " ".join(f"{key}={value}" for key, value in fields if value is not None)
    lines = [f"loopplane scheduler summary: {first}".rstrip()]
    if summary.get("message"):
        lines.append(str(summary["message"]))
    if summary.get("hint"):
        lines.append(f"hint: {summary['hint']}")
    return "\n".join(lines) + "\n"


def completion_marker_status(paths: WorkflowPaths) -> dict[str, Any]:
    marker_path = _completion_marker_path(paths)
    if marker_path is None:
        return _missing_completion_marker()
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "exists": True,
            "fresh": False,
            "path": marker_path.as_posix(),
            "stale_reasons": ["invalid_json"],
        }
    except OSError as error:
        return {
            "exists": True,
            "fresh": False,
            "path": marker_path.as_posix(),
            "stale_reasons": [f"read_error:{error.__class__.__name__}"],
        }
    if not isinstance(marker, Mapping):
        return {
            "exists": True,
            "fresh": False,
            "path": marker_path.as_posix(),
            "stale_reasons": ["marker_not_object"],
        }

    current = _completion_freshness_values(paths, marker)
    stale_reasons: list[str] = []
    audit_drift_reasons: list[str] = []
    for field, current_value in current.items():
        if field not in marker:
            _append_completion_freshness_reason(field, f"{field}_missing", stale_reasons, audit_drift_reasons)
            continue
        marker_value = marker.get(field)
        if marker_value is None and current_value is None:
            continue
        if marker_value is None:
            _append_completion_freshness_reason(field, f"{field}_missing", stale_reasons, audit_drift_reasons)
        elif current_value is None:
            _append_completion_freshness_reason(field, f"{field}_current_unavailable", stale_reasons, audit_drift_reasons)
        elif str(marker_value) != str(current_value):
            _append_completion_freshness_reason(field, f"{field}_mismatch", stale_reasons, audit_drift_reasons)
    return {
        "exists": True,
        "fresh": not stale_reasons,
        "path": marker_path.as_posix(),
        "marker_name": marker_path.name,
        "stale_reasons": stale_reasons,
        "audit_drift_reasons": audit_drift_reasons,
        "current": current,
    }


def _append_completion_freshness_reason(
    field: str,
    reason: str,
    stale_reasons: list[str],
    audit_drift_reasons: list[str],
) -> None:
    if field in {"event_log_head", "state_fingerprint"}:
        audit_drift_reasons.append(reason)
        return
    stale_reasons.append(reason)


def _missing_completion_marker() -> dict[str, Any]:
    return {
        "exists": False,
        "fresh": False,
        "path": None,
        "stale_reasons": [],
    }


def _completion_marker_record_path(paths: WorkflowPaths, marker: Mapping[str, Any]) -> str:
    raw_path = marker.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        path = Path(raw_path)
        if not path.is_absolute():
            path = paths.project_root / path
        return _path_for_record(paths.project_root, path)
    return f"{paths.value('runtime_dir')}/plan_loop_complete.json"


def _scheduler_completion_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
        tasks = []
    total = 0
    completed = 0
    skipped = 0
    blocked = 0
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        total += 1
        status = str(task.get("status") or task.get("checkbox") or "").strip().strip("[]")
        if status == "x":
            completed += 1
        elif status == "-":
            skipped += 1
        elif status == "!":
            blocked += 1
    return {
        "one_line": f"Workflow completed with {completed} completed task(s) and {skipped} skipped task(s).",
        "tasks_total": total,
        "tasks_completed": completed,
        "tasks_blocked": blocked,
        "tasks_skipped": skipped,
    }


def _objective_report_state(
    paths: WorkflowPaths,
    *,
    plan_text: str,
    plan_sha256: str | None,
    objectives: Sequence[ObjectiveRecord],
) -> dict[str, Any]:
    groups = _objective_record_groups(objectives)
    expansion_counts = _objective_expansion_counts(paths)
    reports: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for group in groups:
        scope = str(group["scope"])
        phase_id = group.get("phase_id")
        target_objectives = [item for item in group.get("objectives", []) if isinstance(item, ObjectiveRecord)]
        target_objective_by_id = {objective.objective_id: objective for objective in target_objectives}
        try:
            from runtime.objective_verification import objective_report_path, objective_result_is_closed, objective_result_is_expandable, objective_results_by_id

            path = objective_report_path(paths, scope=scope, phase_id=str(phase_id) if phase_id else None)
        except Exception:
            path = paths.runtime_dir / "objectives" / f"{_safe_scheduler_path_part(_objective_group_key(scope, phase_id))}.json"
            objective_results_by_id = lambda report: {}  # type: ignore[assignment]
            objective_result_is_closed = lambda result: False  # type: ignore[assignment]
            objective_result_is_expandable = lambda result: False  # type: ignore[assignment]
        exists = path.is_file()
        report = _read_json_object(path, default={}) if exists else {}
        result_by_id = objective_results_by_id(report) if isinstance(report, Mapping) else {}
        target_ids = [objective.objective_id for objective in target_objectives]
        covered_ids = [objective_id for objective_id in target_ids if objective_id in result_by_id]
        missing_result_ids = [objective_id for objective_id in target_ids if objective_id not in result_by_id]
        closed_ids = [
            objective_id
            for objective_id in target_ids
            if objective_id in result_by_id and objective_result_is_closed(result_by_id[objective_id])
        ]
        raw_expandable_ids = [
            objective_id
            for objective_id in target_ids
            if objective_id in result_by_id
            and not objective_result_is_closed(result_by_id[objective_id])
            and objective_result_is_expandable(result_by_id[objective_id])
        ]
        blocked_marker_ids = [objective.objective_id for objective in target_objectives if objective.status == "!"]
        expansion_exhausted_ids: list[str] = []
        expandable_ids: list[str] = []
        objective_expansion_limits: dict[str, int] = {}
        for objective_id in raw_expandable_ids:
            objective = target_objective_by_id.get(objective_id)
            if objective is None or objective_id in blocked_marker_ids:
                expansion_exhausted_ids.append(objective_id)
                continue
            max_expansions = _objective_max_expansions(objective)
            objective_expansion_limits[objective_id] = max_expansions
            if expansion_counts.get(objective_id, 0) >= max_expansions:
                expansion_exhausted_ids.append(objective_id)
            else:
                expandable_ids.append(objective_id)
        unresolved_ids = [
            objective_id
            for objective_id in target_ids
            if objective_id not in result_by_id or not objective_result_is_closed(result_by_id[objective_id])
        ]
        marker_closed_ids = [objective.objective_id for objective in target_objectives if objective.status in {"x", "-"}]
        report_plan_sha = str(report.get("plan_sha256") or "") if isinstance(report, Mapping) else ""
        current_structure_fingerprint = objective_structure_fingerprint(
            plan_text,
            objectives=target_objectives,
        )
        report_structure_fingerprint = ""
        if isinstance(report, Mapping):
            report_structure_fingerprint = str(
                report.get("objective_structure_fingerprint")
                or report.get("objective_structure_sha256")
                or ""
            )
        if report_structure_fingerprint:
            fresh = (
                exists
                and bool(report)
                and report_structure_fingerprint == current_structure_fingerprint
                and not missing_result_ids
            )
        else:
            fresh = exists and bool(report) and report_plan_sha == str(plan_sha256 or "") and not missing_result_ids
        if not target_ids:
            status = "empty"
        elif not fresh:
            status = "needs_verification"
        elif set(closed_ids) == set(target_ids):
            status = "closed"
        elif expandable_ids:
            status = "needs_expansion"
        else:
            status = "unresolved"
        record = {
            "key": _objective_group_key(scope, phase_id),
            "scope": scope,
            "phase_id": phase_id,
            "phase_title": group.get("phase_title"),
            "path": _path_for_record(paths.project_root, path),
            "exists": exists,
            "fresh": fresh,
            "status": status,
            "target_objective_ids": target_ids,
            "marker_closed_objective_ids": marker_closed_ids,
            "covered_objective_ids": covered_ids,
            "missing_result_objective_ids": missing_result_ids,
            "closed_objective_ids": closed_ids,
            "expandable_objective_ids": expandable_ids,
            "raw_expandable_objective_ids": raw_expandable_ids,
            "expansion_exhausted_objective_ids": expansion_exhausted_ids,
            "blocked_marker_objective_ids": blocked_marker_ids,
            "objective_expansion_counts": {objective_id: expansion_counts.get(objective_id, 0) for objective_id in target_ids},
            "objective_expansion_limits": objective_expansion_limits,
            "unresolved_objective_ids": unresolved_ids,
        }
        if isinstance(report, Mapping) and report:
            record["report_status"] = report.get("status")
            record["report_verified_at"] = report.get("verified_at")
            record["report_plan_sha256"] = report.get("plan_sha256")
            record["report_accepted_plan_sha256"] = report.get("accepted_plan_sha256")
            record["report_objective_structure_fingerprint"] = report_structure_fingerprint or None
            record["current_objective_structure_fingerprint"] = current_structure_fingerprint
            record["report"] = _json_safe_object(report)
        reports.append(record)
        by_key[record["key"]] = record
    return {
        "schema_version": SCHEMA_VERSION,
        "reports": reports,
        "by_key": by_key,
        "summary": {
            "total_objectives": len(objectives),
            "phase_objectives": sum(1 for objective in objectives if objective.scope == "phase"),
            "workflow_objectives": sum(1 for objective in objectives if objective.scope == "workflow"),
            "closed_reports": sum(1 for report in reports if report.get("status") == "closed"),
            "needs_verification": sum(1 for report in reports if report.get("status") == "needs_verification"),
            "needs_expansion": sum(1 for report in reports if report.get("status") == "needs_expansion"),
            "unresolved": sum(1 for report in reports if report.get("status") == "unresolved"),
        },
    }


def _objective_record_groups(objectives: Sequence[ObjectiveRecord]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for objective in objectives:
        if objective.scope not in {"phase", "workflow"}:
            continue
        key = _objective_group_key(objective.scope, objective.phase_id)
        if key not in groups:
            groups[key] = {
                "scope": objective.scope,
                "phase_id": objective.phase_id,
                "phase_title": objective.phase_title,
                "objectives": [],
                "first_line_index": objective.line_index,
            }
            order.append(key)
        groups[key]["objectives"].append(objective)
    return [groups[key] for key in sorted(order, key=lambda item: int(groups[item].get("first_line_index") or 0))]


def _objective_expansion_counts(paths: WorkflowPaths) -> dict[str, int]:
    registry = _read_json_object(paths.runtime_dir / "expansion_registry.json", default={})
    proposals = registry.get("proposals") if isinstance(registry, Mapping) else []
    counts: dict[str, int] = {}
    if not isinstance(proposals, Sequence) or isinstance(proposals, (str, bytes)):
        return counts
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            continue
        if str(proposal.get("expansion_type") or "") != "objective_gap":
            continue
        if str(proposal.get("status") or "") not in {"applied", "applied_with_warnings"}:
            continue
        target_ids = proposal.get("target_objective_ids")
        if not isinstance(target_ids, Sequence) or isinstance(target_ids, (str, bytes)):
            continue
        for objective_id in target_ids:
            clean_id = str(objective_id)
            if clean_id:
                counts[clean_id] = counts.get(clean_id, 0) + 1
    return counts


def _objective_max_expansions(objective: ObjectiveRecord) -> int:
    values = objective.fields.get("max_expansions") or ()
    for value in values:
        try:
            parsed = int(str(value).strip())
        except ValueError:
            continue
        return max(0, parsed)
    return DEFAULT_OBJECTIVE_MAX_EXPANSIONS


def _phase_objective_gate_candidate(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    if not snapshot.get("plan_active"):
        return None
    phase_objectives = [objective for objective in _snapshot_objectives(snapshot) if objective.get("scope") == "phase"]
    if not phase_objectives:
        return None
    tasks = [task for task in snapshot.get("tasks", []) if isinstance(task, Mapping)]
    for phase in _phase_gate_order(tasks, phase_objectives):
        phase_id = str(phase.get("phase_id") or "")
        phase_title = str(phase.get("phase_title") or "")
        phase_tasks = _tasks_for_phase(tasks, phase_id=phase_id, phase_title=phase_title)
        if not phase_tasks:
            return None
        if not _tasks_all_terminal_for_objective_gate(phase_tasks):
            return None
        gate = _objective_gate_record(snapshot, scope="phase", phase_id=phase_id)
        if gate is None or str(gate.get("status") or "") in {"closed", "empty"}:
            continue
        return _objective_gate_selected(snapshot, gate)
    return None


def _final_objective_gate_candidate(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    if not snapshot.get("plan_active"):
        return None
    objectives = [objective for objective in _snapshot_objectives(snapshot) if objective.get("scope") == "workflow"]
    if not objectives:
        return None
    tasks = [task for task in snapshot.get("tasks", []) if isinstance(task, Mapping)]
    if not tasks or not _tasks_all_terminal_for_objective_gate(tasks):
        return None
    phase_gate = _phase_objective_gate_candidate(snapshot)
    if phase_gate is not None:
        return None
    gate = _objective_gate_record(snapshot, scope="workflow", phase_id=None)
    if gate is None or str(gate.get("status") or "") in {"closed", "empty"}:
        return None
    return _objective_gate_selected(snapshot, gate)


def _objective_gate_selected(snapshot: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    scope = str(gate.get("scope") or "")
    phase_id = str(gate.get("phase_id") or "") or None
    action_name = "run_phase_objective_verifier" if scope == "phase" else "run_final_objective_verifier"
    return {
        "role": "objective_verifier",
        "scope": scope,
        "phase_id": phase_id,
        "phase_title": gate.get("phase_title"),
        "run_kind": f"{scope}_objective_verification",
        "objective_gate_status": gate.get("status"),
        "target_objective_ids": list(gate.get("target_objective_ids") or []),
        "objective_verification_report": gate.get("path"),
        "objective_report": dict(gate),
        "action_name": action_name,
    }


def _objective_gate_record(snapshot: Mapping[str, Any], *, scope: str, phase_id: str | None) -> dict[str, Any] | None:
    reports = snapshot.get("objective_reports")
    if not isinstance(reports, Mapping):
        return None
    by_key = reports.get("by_key")
    if not isinstance(by_key, Mapping):
        return None
    gate = by_key.get(_objective_group_key(scope, phase_id))
    return dict(gate) if isinstance(gate, Mapping) else None


def _snapshot_objectives(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = snapshot.get("objectives")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [dict(item) for item in values if isinstance(item, Mapping)]


def _phase_gate_order(tasks: Sequence[Mapping[str, Any]], phase_objectives: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for objective in phase_objectives:
        phase_id = str(objective.get("phase_id") or "")
        phase_title = str(objective.get("phase_title") or "")
        key = _phase_match_key(phase_id=phase_id, phase_title=phase_title)
        if key not in by_key:
            by_key[key] = {"phase_id": phase_id, "phase_title": phase_title}
            order.append(key)
    task_phase_order: list[str] = []
    for task in tasks:
        phase_title = str(task.get("phase") or "")
        key = _phase_match_key(phase_id=_phase_id_from_title(phase_title), phase_title=phase_title)
        if key and key not in task_phase_order:
            task_phase_order.append(key)
    def sort_key(key: str) -> tuple[int, int]:
        if key in task_phase_order:
            return (0, task_phase_order.index(key))
        return (1, order.index(key))
    return [by_key[key] for key in sorted(order, key=sort_key)]


def _tasks_for_phase(tasks: Sequence[Mapping[str, Any]], *, phase_id: str, phase_title: str) -> list[Mapping[str, Any]]:
    matches: list[Mapping[str, Any]] = []
    for task in tasks:
        task_phase = str(task.get("phase") or "")
        if task_phase == phase_title:
            matches.append(task)
            continue
        if phase_id and _phase_id_from_title(task_phase) == phase_id:
            matches.append(task)
    return matches


def _tasks_all_terminal_for_objective_gate(tasks: Sequence[Mapping[str, Any]]) -> bool:
    return bool(tasks) and all(str(task.get("status") or "") in {"x", "!", "-"} for task in tasks)


def _objective_group_key(scope: str, phase_id: object | None) -> str:
    return f"phase:{phase_id}" if scope == "phase" else "workflow"


def _phase_match_key(*, phase_id: str, phase_title: str) -> str:
    return phase_id or phase_title


def _phase_id_from_title(phase_title: str) -> str:
    match = re.match(r"^Phase\s+([^:]+)", phase_title)
    if match:
        return match.group(1).strip()
    return phase_title.strip()


def _safe_scheduler_path_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._") or "objective"


def _completion_marker_path(paths: WorkflowPaths) -> Path | None:
    for name in ("plan_loop_complete.json", "completion_marker.json"):
        path = paths.runtime_dir / name
        if path.exists():
            return path
    return None


def _completion_freshness_values(paths: WorkflowPaths, marker: Mapping[str, Any]) -> dict[str, str | None]:
    final_report_path = _project_relative_path(paths.project_root, marker.get("final_verification_report"))
    if final_report_path is None:
        final_report_path = _project_relative_path(paths.project_root, marker.get("final_verification_path"))
    values = {
        "plan_sha256": _sha256_file(paths.plan_file),
        "evidence_manifest_sha256": _sha256_file(paths.runtime_dir / "evidence_manifest.json"),
        "event_log_head": _event_log_head(paths.runtime_dir / "events"),
        "final_verification_report_sha256": _sha256_file(final_report_path) if final_report_path else None,
        "final_git_checkpoint_id": _latest_git_checkpoint_id(paths.runtime_dir / "git_checkpoints.jsonl"),
    }
    objective_closure_sha = _objective_completion_fingerprint(paths, marker)
    if objective_closure_sha is not None:
        values["objective_closure_sha256"] = objective_closure_sha
    values["state_fingerprint"] = _state_fingerprint(values)
    return values


def _objective_completion_fingerprint(paths: WorkflowPaths, marker: Mapping[str, Any]) -> str | None:
    plan_text = _read_text(paths.plan_file)
    objectives, _errors = parse_plan_objectives(plan_text)
    if not objectives and "objective_closure_sha256" not in marker:
        return None
    report_paths: dict[str, str] = {}
    try:
        from runtime.objective_verification import objective_report_path

        for objective in objectives:
            report_path = objective_report_path(paths, scope=objective.scope, phase_id=objective.phase_id)
            if report_path.exists():
                report_paths[objective.objective_id] = _path_for_record(paths.project_root, report_path)
    except Exception:
        report_paths = {}
    return objective_closure_fingerprint(plan_text, project_root=paths.project_root, report_paths=report_paths)


def _project_relative_path(project_root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return project_root / value


def _sha256_file(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return "sha256:" + sha256(data).hexdigest()


def _event_log_head(events_dir: Path) -> str | None:
    segments = sorted(events_dir.glob("*.jsonl"))
    if not segments:
        return None
    last_record: dict[str, Any] | None = None
    last_segment: Path | None = None
    last_line_hash: str | None = None
    for segment in segments:
        try:
            lines = segment.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            last_segment = segment
            last_line_hash = "sha256:" + sha256(line.encode("utf-8")).hexdigest()
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            last_record = dict(record) if isinstance(record, Mapping) else None
    if last_segment is None:
        return None
    if last_record is not None:
        event_id = last_record.get("event_id") or last_record.get("id")
        if isinstance(event_id, str) and event_id:
            return event_id
        sequence = last_record.get("sequence")
        if sequence is not None:
            return f"{last_segment.name}:sequence:{sequence}"
    return f"{last_segment.name}:{last_line_hash}"


def _latest_git_checkpoint_id(path: Path) -> str | None:
    records = _read_jsonl(path)
    for record in reversed(records):
        checkpoint_id = record.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            return checkpoint_id
    return None


def _state_fingerprint(values: Mapping[str, str | None]) -> str:
    state_values = {
        "plan_sha256": values.get("plan_sha256"),
        "evidence_manifest_sha256": values.get("evidence_manifest_sha256"),
        "event_log_head": values.get("event_log_head"),
        "final_verification_report_sha256": values.get("final_verification_report_sha256"),
        "final_git_checkpoint_id": values.get("final_git_checkpoint_id"),
    }
    if "objective_closure_sha256" in values:
        state_values["objective_closure_sha256"] = values.get("objective_closure_sha256")
    encoded = json.dumps(state_values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _preview_selected(snapshot: Mapping[str, Any], action: Mapping[str, Any]) -> dict[str, Any]:
    selected = _json_safe_object(action.get("selected", {}))
    paths = snapshot.get("paths")
    if not isinstance(selected, dict) or not isinstance(paths, WorkflowPaths):
        return selected if isinstance(selected, dict) else {}
    action_name = str(action.get("action"))
    if action_name in {"run_worker", "run_recovery"}:
        selected.setdefault("expected_prompt_path", f"{paths.value('runtime_dir')}/runs/<run_id>/prompt.md")
        selected.setdefault("scheduler_run_dir", f"{paths.value('runtime_dir')}/runs/<run_id>")
        task_id = selected.get("task_id")
        if isinstance(task_id, str) and task_id:
            selected.setdefault("task_evidence_run_dir", f"{paths.value('results_dir')}/{task_id}/runs/<run_id>")
            selected.setdefault("role_output_dir", f"{paths.value('results_dir')}/{task_id}/runs/<run_id>")
        selected.setdefault("run_id_allocated", False)
        selected.setdefault("prompt_path_allocated", False)
    if action_name == "run_final_verification":
        selected.setdefault("expected_prompt_path", f"{paths.value('runtime_dir')}/runs/<run_id>/prompt.md")
        selected.setdefault("scheduler_run_dir", f"{paths.value('runtime_dir')}/runs/<run_id>")
        selected.setdefault("run_id_allocated", False)
        selected.setdefault("prompt_path_allocated", False)
    if action_name == "run_expansion_planner":
        selected.setdefault("expected_prompt_path", f"{paths.value('runtime_dir')}/runs/<run_id>/prompt.md")
        selected.setdefault("scheduler_run_dir", f"{paths.value('runtime_dir')}/runs/<run_id>")
        selected.setdefault("role_output_dir", f"{paths.value('runtime_dir')}/expansions/<run_id>")
        selected.setdefault("proposal_path", f"{paths.value('runtime_dir')}/expansions/<run_id>/{EXPANSION_PROPOSAL_FILENAME}")
        selected.setdefault("run_id_allocated", False)
        selected.setdefault("prompt_path_allocated", False)
    if action_name in {"run_phase_objective_verifier", "run_final_objective_verifier"}:
        selected.setdefault("expected_prompt_path", f"{paths.value('runtime_dir')}/runs/<run_id>/prompt.md")
        selected.setdefault("scheduler_run_dir", f"{paths.value('runtime_dir')}/runs/<run_id>")
        selected.setdefault("role_output_dir", f"{paths.value('runtime_dir')}/objectives/<run_id>")
        selected.setdefault("objective_verification_report", f"{paths.value('runtime_dir')}/objectives/<run_id>/objective_verification.json")
        selected.setdefault("run_id_allocated", False)
        selected.setdefault("prompt_path_allocated", False)
    if action.get("would_wait"):
        selected.setdefault("blocking_reason", action.get("reason"))
    return selected


def _json_safe_object(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_object(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_object(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


_CANDIDATE_SKIP_REASONS = {
    "control_request": "No pending control request needed scheduler handling.",
    "runtime_control_state": "Runtime control state does not pause or stop scheduling.",
    "approval": "No pending approval request blocked execution.",
    "active_run_lease": "No active run lease blocked the next scheduler step.",
    "background_job": "No active background job blocked the next scheduler step.",
    "config_wait": "No configuration wait condition was selected.",
    "recoverable_failure": "No recoverable failure within retry budget was selected.",
    "self_expansion_resolution": "No applied expansion proposal is ready to reopen a target failure.",
    "self_expansion_recovery_exhausted": "No exhausted recoverable failure required self-expansion before later work.",
    "worker_task": "No executable worker task was selected.",
    "runner_availability": "No active runner availability hold blocked execution.",
    "completion_marker": "No fresh completion marker was available.",
    "self_expansion_final_failure": "No expandable final verification failure required expansion.",
    "self_expansion_no_executable": "Self-expansion had no no-executable-work trigger.",
}


def _preview_skipped_candidates(considered: object) -> list[dict[str, str]]:
    if not isinstance(considered, Sequence) or isinstance(considered, (str, bytes)):
        return []
    skipped: list[dict[str, str]] = []
    for item in considered:
        if not isinstance(item, Mapping):
            continue
        candidate = str(item.get("candidate") or "unknown")
        result = str(item.get("result") or "not_selected")
        skipped.append(
            {
                "candidate": candidate,
                "result": result,
                "reason": _CANDIDATE_SKIP_REASONS.get(candidate, f"Candidate {candidate} was not selected."),
            }
        )
    return skipped


def _action(
    action: str,
    *,
    reason: str,
    selected: Mapping[str, Any],
    would_wait: bool = False,
    blocking_conditions: Sequence[str] = (),
    considered: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "action": action,
        "reason": reason,
        "selected": dict(selected),
        "would_wait": would_wait,
        "blocking_conditions": list(blocking_conditions),
        "considered": [dict(item) for item in considered],
    }


def _next_control_request(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    responded = {
        control_record_id(response)
        for response in snapshot.get("control_responses", [])
        if isinstance(response, Mapping) and control_record_id(response)
    }
    for request in snapshot.get("control_requests", []):
        request_id = control_record_id(request) if isinstance(request, Mapping) else None
        status = str(request.get("status", "pending")).lower()
        if request_id in responded or status in {"handled", "completed", "cancelled"}:
            continue
        return request
    return None


def _runtime_control_wait(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    state = snapshot.get("state")
    if not isinstance(state, Mapping):
        return None
    scheduler = state.get("scheduler")
    scheduler_state = scheduler if isinstance(scheduler, Mapping) else {}
    status = str(state.get("status") or "").lower()
    if status == "paused" or scheduler_state.get("paused") is True:
        return {
            "action": "wait_paused",
            "reason": "Workflow execution is paused by control request.",
            "selected": {
                "run_kind": "control_wait",
                "type": "pause",
                "status": "paused",
                "last_control_request_id": scheduler_state.get("last_control_request_id"),
            },
            "blocking_conditions": ["paused"],
        }
    if status == "stopped" or scheduler_state.get("stop_requested") is True:
        return {
            "action": "wait_stopped",
            "reason": "Workflow execution is stopped by control request.",
            "selected": {
                "run_kind": "control_wait",
                "type": "stop",
                "status": "stopped",
                "last_control_request_id": scheduler_state.get("last_control_request_id"),
            },
            "blocking_conditions": ["stopped"],
        }
    return None


def _approval_gate(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    policy = _approval_policy(snapshot)
    approval_enabled = policy.get("enabled") is True
    responses = [response for response in snapshot.get("approval_responses", []) if isinstance(response, Mapping)]
    requests = [request for request in snapshot.get("approval_requests", []) if isinstance(request, Mapping)]

    if not approval_enabled:
        task = _next_approval_required_task(snapshot)
        if task is None or not task.get("requires_human_approval"):
            return None
        task_id = str(task.get("task_id") or "")
        decision = task_approval_decision(responses, task_id)
        if decision == "approved":
            return None
        if decision in {"rejected", "expired", "superseded"}:
            return _approval_requires_attention(
                reason=f"The next executable task approval was {decision}.",
                selected={
                    "task_id": task_id,
                    "run_kind": f"approval_{decision}",
                    "type": f"approval_{decision}",
                    "message": f"Task {task_id} cannot run because its approval was {decision}.",
                    "approval_decision": decision,
                },
            )
        return _approval_requires_attention(
            reason="The next executable task requires human approval, but interactive approvals are disabled.",
            selected={
                "task_id": task_id,
                "run_kind": "approval_disabled",
                "type": "approval_disabled",
                "message": (
                    f"Task {task_id} requires human approval, but interactive approvals are disabled "
                    "in security.json; enable approvals or record an explicit approved response before running it."
                ),
                "approval_policy": policy,
            },
        )

    for request in requests:
        status_record = approval_record_status(request, responses=responses, now=utc_timestamp())
        if status_record.get("status") != "pending":
            continue
        request_id = _record_id(status_record)
        return {
            "action": "wait_approval",
            "reason": "A human approval request is pending.",
            "selected": {"approval_id": request_id, "approval": status_record, "run_kind": "approval_wait"},
            "blocking_conditions": ["waiting_approval"],
        }

    task = _next_approval_required_task(snapshot)
    if task is None or not task.get("requires_human_approval"):
        return None
    task_id = str(task.get("task_id") or "")
    decision = task_approval_decision(responses, task_id)
    if decision == "approved":
        return None
    if decision in {"rejected", "expired", "superseded"}:
        return _approval_requires_attention(
            reason=f"The next executable task approval was {decision}.",
            selected={
                "task_id": task_id,
                "run_kind": f"approval_{decision}",
                "type": f"approval_{decision}",
                "message": f"Task {task_id} cannot run because its approval was {decision}.",
                "approval_decision": decision,
            },
        )
    existing = _pending_approval_request_for_task(requests, responses, task_id)
    if existing is not None:
        return {
            "action": "wait_approval",
            "reason": "The next executable task has a pending human approval request.",
            "selected": {
                "approval_id": _record_id(existing),
                "approval": existing,
                "task_id": task_id,
                "run_kind": "approval_wait",
            },
            "blocking_conditions": ["waiting_approval"],
        }
    return {
        "action": "wait_approval",
        "reason": "The next executable task requires human approval.",
        "selected": {
            "task_id": task_id,
            "task": dict(task),
            "run_kind": "approval_wait",
            "approval_request_needed": True,
        },
        "blocking_conditions": ["waiting_approval"],
    }


def _approval_requires_attention(*, reason: str, selected: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action": "requires_attention",
        "reason": reason,
        "selected": dict(selected),
        "blocking_conditions": ["requires_attention", "approval_required"],
    }


def _approval_policy(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    policy = snapshot.get("approval_policy")
    if isinstance(policy, Mapping):
        return dict(policy)
    return {"schema_version": SCHEMA_VERSION, "enabled": False, "mode": "disabled", "default_action_when_disabled": "auto_authorize"}


def _pending_approval_request_for_task(
    requests: Sequence[Mapping[str, Any]],
    responses: Sequence[Mapping[str, Any]],
    task_id: str,
) -> dict[str, Any] | None:
    for request in requests:
        if str(request.get("task_id") or "") != task_id:
            continue
        status_record = approval_record_status(request, responses=responses, now=utc_timestamp())
        if status_record.get("status") == "pending":
            return status_record
    return None


def _next_approval_required_task(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    tasks = [task for task in snapshot.get("tasks", []) if isinstance(task, Mapping)]
    completed = {str(task.get("task_id")) for task in tasks if str(task.get("status")) in {"x", "-"}}
    for task in tasks:
        status = str(task.get("status"))
        if status not in {" ", "~"}:
            continue
        dependencies = [str(dep) for dep in task.get("depends_on", [])]
        if any(dep not in completed for dep in dependencies):
            continue
        if task.get("requires_human_approval"):
            return task
        return None
    return None


def _background_job_not_ready(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    deferred_problem_jobs: list[Mapping[str, Any]] = []
    for job in snapshot.get("background_jobs", []):
        status = str(job.get("status", "running")).lower()
        next_prompt_ready = job.get("next_prompt_ready")
        if status in BACKGROUND_JOB_SAFE_STATUSES and next_prompt_ready is not False:
            continue
        if status in {"pending", "running"}:
            return job
        if status in ALLOWED_BACKGROUND_JOB_STATUSES or next_prompt_ready is False:
            deferred_problem_jobs.append(job)
            continue
        deferred_problem_jobs.append(
            {
                **dict(job),
                "status": "needs_recovery",
                "status_problem": f"invalid_status:{status}",
            }
        )
    # A historical failed job may already have been recovered by launching a
    # replacement background job. Registry insertion order must not let that
    # terminal record mask the live replacement and dispatch a duplicate
    # worker. Active work is selected above; only then consider terminal or
    # malformed records for recovery/attention handling.
    return deferred_problem_jobs[0] if deferred_problem_jobs else None


def _background_job_attention_problem(job: Mapping[str, Any]) -> dict[str, str] | None:
    status = str(job.get("status") or "").strip().lower()
    if status not in BACKGROUND_JOB_ATTENTION_STATUSES:
        return None
    job_id = _record_id(job) or "background_job"
    status_problem = str(job.get("status_problem") or "").strip()
    problem_suffix = f" ({status_problem})" if status_problem else ""
    return {
        "blocking_condition": f"background_job_{status}",
        "reason": (
            f"Background job {job_id} is {status}{problem_suffix}. "
            "It cannot become safe by scheduler polling alone; recover or manually resolve the background job before continuing."
        ),
    }


def _failure_for_background_job(
    snapshot: Mapping[str, Any], job: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    job_id = _record_id(job)
    if not job_id:
        return None
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("source_background_job_id") or "") == job_id:
            return failure
    return None


def _active_run_lease_not_ready(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for lease in snapshot.get("active_run_leases", []):
        if not isinstance(lease, Mapping):
            continue
        if not _lease_blocks_scheduler(lease):
            continue
        status = str(lease.get("status") or "running").lower()
        if status in ACTIVE_RUN_LEASE_INACTIVE_STATUSES:
            continue
        return lease
    return None


def _active_run_lease_attention_problem(lease: Mapping[str, Any]) -> dict[str, str] | None:
    status = str(lease.get("status") or "").strip().lower()
    status_problem = str(lease.get("status_problem") or "").strip().lower()
    if status == "needs_recovery" and status_problem == "malformed_lease":
        return {
            "blocking_condition": "active_run_lease_malformed",
            "reason": "An active run lease is malformed and must be repaired before scheduling can safely continue.",
        }
    liveness = str(lease.get("runner_liveness") or "").strip().lower()
    if liveness == "dead" and status in {"stale", "needs_recovery"}:
        return {
            "blocking_condition": "active_run_lease_stale_dead",
            "reason": "An active run lease is stale and no recorded runner process is alive.",
        }
    return None


def _lease_blocks_scheduler(lease: Mapping[str, Any]) -> bool:
    if lease.get("blocks_scheduler") is False:
        return False
    return str(lease.get("role") or "").strip().lower() != "inspector"


def _config_wait(snapshot: Mapping[str, Any]) -> dict[str, str] | None:
    problems = snapshot.get("configuration_problems") or []
    if problems:
        first = problems[0]
        if isinstance(first, Mapping):
            return {
                "code": str(first.get("code") or "configuration_problem"),
                "message": str(first.get("message") or first.get("reason") or "Workflow configuration requires attention."),
            }
        return {"code": "configuration_problem", "message": str(first)}
    if not snapshot.get("plan_active"):
        return {"code": "active_plan_missing", "message": "No active plan has been activated."}
    return None


def _oldest_recoverable_failure(snapshot: Mapping[str, Any]) -> Mapping[str, Any] | None:
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    candidates: list[tuple[str, int, Mapping[str, Any]]] = []
    for index, failure in enumerate(failures):
        if not isinstance(failure, Mapping):
            continue
        # Non-task action failures (objective verifier / expansion planner) are
        # retried by re-selecting their own action, not by the task-keyed
        # recovery_worker path, so they must not be selected here.
        if str(failure.get("failure_class") or "") in ACTION_FAILURE_CLASSES:
            continue
        status = str(failure.get("status", "unrecovered")).lower()
        if status != "unrecovered":
            continue
        if failure.get("recoverable") is False:
            continue
        attempts = _int_value(failure.get("recovery_attempts", failure.get("attempts", 0)), default=0)
        budget = _int_value(
            failure.get("max_recovery_attempts", failure.get("max_attempts", DEFAULT_MAX_RECOVERY_ATTEMPTS)),
            default=DEFAULT_MAX_RECOVERY_ATTEMPTS,
        )
        if attempts >= budget:
            continue
        created = str(failure.get("first_seen_at") or failure.get("created_at") or failure.get("detected_at") or f"{index:08d}")
        candidates.append((created, index, failure))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]


def _next_executable_task(
    snapshot: Mapping[str, Any],
    *,
    allow_approval_required: bool = False,
) -> Mapping[str, Any] | None:
    tasks = [task for task in snapshot.get("tasks", []) if isinstance(task, Mapping)]
    completed = {str(task.get("task_id")) for task in tasks if str(task.get("status")) in {"x", "-"}}
    for task in tasks:
        status = str(task.get("status"))
        if status not in {" ", "~"}:
            continue
        task_id = str(task.get("task_id") or "")
        if _task_has_unresolved_failure(snapshot, task_id):
            continue
        dependencies = [str(dep) for dep in task.get("depends_on", [])]
        if any(dep not in completed for dep in dependencies):
            continue
        if task.get("requires_human_approval") and _task_approval_recorded(snapshot, task_id):
            return task
        if task.get("requires_human_approval") and not allow_approval_required:
            continue
        if task.get("requires_human_approval") and allow_approval_required:
            return task
        return task
    return None


def _task_has_unresolved_failure(snapshot: Mapping[str, Any], task_id: str) -> bool:
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("task_id") or "") != task_id:
            continue
        if str(failure.get("status") or "unrecovered") not in {"recovered", "waived"}:
            return True
    return False


def _final_verification_candidate(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    if not snapshot.get("plan_active"):
        return None
    tasks = [task for task in snapshot.get("tasks", []) if isinstance(task, Mapping)]
    if not tasks:
        return None
    terminal_statuses = {"x", "-"}
    if all(str(task.get("status")) in terminal_statuses for task in tasks):
        return {
            "role": "final_verifier",
            "run_kind": "final_verification",
            "task_count": len(tasks),
        }
    return None


def _runner_for_role(snapshot: Mapping[str, Any], role: str) -> RunnerConfig | None:
    candidates = _runner_candidates_for_role(snapshot, role)
    if not candidates:
        return None
    failover_candidates = _runner_failover_candidates(snapshot, role)
    if failover_candidates:
        available = [runner for runner in failover_candidates if _active_hold_for_runner(snapshot, runner) is None]
        healthy = [
            runner
            for runner in available
            if not _runner_is_unhealthy_for_failover(snapshot, runner.runner_id, role)
        ]
        if healthy:
            return healthy[0]
        return available[0] if available else None
    for runner in candidates:
        if _active_hold_for_runner(snapshot, runner) is None:
            return runner
    return None


def _runner_candidates_for_role(snapshot: Mapping[str, Any], role: str) -> tuple[RunnerConfig, ...]:
    config = snapshot.get("runner_config")
    if config is None:
        return ()
    matches = config.runners_for_role(role, enabled_only=True)
    if not matches:
        return ()
    failover_candidates = _runner_failover_candidates(snapshot, role)
    if failover_candidates:
        return failover_candidates
    workflow_config = snapshot.get("workflow_config")
    if role == "worker" and isinstance(workflow_config, Mapping):
        preferred = workflow_config.get("default_worker_runner")
        ordered: list[RunnerConfig] = []
        for runner in matches:
            if runner.runner_id == preferred:
                ordered.append(runner)
        ordered.extend(runner for runner in matches if runner.runner_id != preferred)
        return tuple(ordered)
    return tuple(matches)


def _active_hold_for_roles(snapshot: Mapping[str, Any], roles: Sequence[str]) -> dict[str, Any] | None:
    for role in roles:
        for runner in _runner_candidates_for_role(snapshot, str(role)):
            hold = _active_hold_for_runner(snapshot, runner)
            if hold is not None:
                return hold
    return None


def _active_hold_for_runner(snapshot: Mapping[str, Any], runner: RunnerConfig) -> dict[str, Any] | None:
    runner_health = snapshot.get("runner_health")
    runners = runner_health.get("runners", {}) if isinstance(runner_health, Mapping) else {}
    if not isinstance(runners, Mapping):
        return None
    candidate_scope = _runner_availability_scope(runner)
    for record_runner_id, record in runners.items():
        if not isinstance(record, Mapping):
            continue
        hold = record.get("availability_hold")
        if not _availability_hold_is_active(hold):
            continue
        hold_scope = hold.get("scope") if isinstance(hold, Mapping) else None
        if _same_availability_scope(candidate_scope, hold_scope) or str(record_runner_id) == runner.runner_id:
            result = dict(hold)
            result.setdefault("source_runner_id", str(record_runner_id))
            result.setdefault("matched_runner_id", runner.runner_id)
            return result
    return None


def _availability_hold_is_active(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "active":
        return False
    if value.get("requires_attention") is True:
        return True
    cooldown_until = _parse_iso_timestamp(value.get("cooldown_until"))
    if cooldown_until is None:
        return True
    return cooldown_until > datetime.now(UTC)


def _runner_availability_scope(runner: RunnerConfig) -> dict[str, str]:
    options = runner.adapter_options if isinstance(runner.adapter_options, Mapping) else {}
    policy = None
    for key in ("runner_availability", "availability_policy"):
        candidate = options.get(key)
        if isinstance(candidate, Mapping):
            policy = candidate
            break
    raw_scope = policy.get("scope") if isinstance(policy, Mapping) else None
    if isinstance(raw_scope, Mapping):
        scope_type = str(raw_scope.get("type") or "runner").strip().lower()
        scope_key = str(raw_scope.get("key") or "").strip()
        if scope_type in {"runner", "credential", "provider"} and scope_key:
            return {"type": scope_type, "key": scope_key}
    return {"type": "runner", "key": runner.runner_id}


def _same_availability_scope(left: Mapping[str, Any], right: Any) -> bool:
    if not isinstance(right, Mapping):
        return False
    return str(left.get("type") or "") == str(right.get("type") or "") and str(left.get("key") or "") == str(right.get("key") or "")


def _runner_failover_candidates(snapshot: Mapping[str, Any], role: str) -> tuple[RunnerConfig, ...]:
    config = snapshot.get("runner_config")
    failover = getattr(config, "runner_failover", None)
    if not isinstance(failover, Mapping):
        return ()
    rule = failover.get(role)
    if not isinstance(rule, Mapping) or rule.get("strategy", "ordered") != "ordered":
        return ()
    ordered_ids = rule.get("runners")
    if not isinstance(ordered_ids, Sequence) or isinstance(ordered_ids, (str, bytes)):
        return ()
    candidates: list[RunnerConfig] = []
    seen: set[str] = set()
    for runner_id in ordered_ids:
        if not isinstance(runner_id, str) or runner_id in seen:
            continue
        seen.add(runner_id)
        runner = getattr(config, "runners", {}).get(runner_id)
        if not isinstance(runner, RunnerConfig) or not runner.enabled:
            continue
        if runner.role != role:
            continue
        candidates.append(runner)
    return tuple(candidates)


def _runner_is_unhealthy_for_failover(snapshot: Mapping[str, Any], runner_id: str, role: str) -> bool:
    config = snapshot.get("runner_config")
    failover = getattr(config, "runner_failover", None)
    rule = failover.get(role) if isinstance(failover, Mapping) else None
    if not isinstance(rule, Mapping):
        return False
    threshold = _positive_config_int(rule.get("mark_unhealthy_after"))
    window_seconds = _positive_config_int(rule.get("failure_window_seconds"))
    if threshold is None or window_seconds is None:
        return False
    runner_health = snapshot.get("runner_health")
    runners = runner_health.get("runners", {}) if isinstance(runner_health, Mapping) else {}
    runner_record = runners.get(runner_id) if isinstance(runners, Mapping) else None
    events = runner_record.get("events", []) if isinstance(runner_record, Mapping) else []
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return False
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=window_seconds)
    recent: list[tuple[datetime, Mapping[str, Any]]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        observed_at = _parse_iso_timestamp(event.get("observed_at") or event.get("ended_at") or event.get("ts"))
        if observed_at is None or observed_at < cutoff:
            continue
        recent.append((observed_at, event))
    consecutive_runner_failures = 0
    for _observed_at, event in sorted(recent, key=lambda item: item[0]):
        if event.get("scope") == "runner_failure":
            consecutive_runner_failures += 1
        else:
            consecutive_runner_failures = 0
    return consecutive_runner_failures >= threshold


def _positive_config_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = _int_value(value, default=0)
    return parsed if parsed > 0 else None


def _handle_control_request(
    paths: WorkflowPaths,
    snapshot: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    owner: str,
) -> dict[str, Any]:
    action = str(request.get("action") or request.get("type") or "unknown").strip().lower().replace("-", "_")
    request_id = control_record_id(request) or f"control_{uuid.uuid4().hex[:8]}"
    current_state = snapshot.get("state") if isinstance(snapshot.get("state"), Mapping) else {}
    current_status = str(current_state.get("status") or "unknown")
    response_status = "applied" if action in CONTROL_REQUEST_TYPES else "rejected"
    message = "Control request applied."
    response_details: dict[str, Any] = {}
    scheduler_update: dict[str, Any] = {
        "last_action": "handle_control_request",
        "last_control_request_id": request_id,
        "last_control_type": action,
        "owner": owner,
    }
    runtime_status: str | None = None
    clear_resolved_attention = False
    if response_status == "rejected":
        message = f"Unsupported control request type: {action}."
    elif action == "start":
        runtime_status = "running"
        clear_resolved_attention = True
        scheduler_update["running"] = True
        scheduler_update["paused"] = False
        scheduler_update["stop_requested"] = False
        scheduler_update["started_at"] = utc_timestamp()
        payload = request.get("payload") if isinstance(request.get("payload"), Mapping) else {}
        scheduler_update["detach_requested"] = bool(payload.get("detach")) if isinstance(payload, Mapping) else False
    elif action == "pause":
        runtime_status = "paused"
        scheduler_update["paused"] = True
        scheduler_update["running"] = False
    elif action == "resume":
        runtime_status = "running"
        clear_resolved_attention = True
        scheduler_update["paused"] = False
        scheduler_update["running"] = True
        scheduler_update["stop_requested"] = False
    elif action == "stop":
        runtime_status = "stopped"
        scheduler_update["stop_requested"] = True
        scheduler_update["running"] = False
    elif action == "attach":
        scheduler_update["last_attach_request_id"] = request_id
        scheduler_update["attached_at"] = utc_timestamp()
        message = "Attach request recorded; use status and logs for current runtime state."
    elif action == "migrate":
        scheduler_update["last_migration_request_id"] = request_id
        scheduler_update["migration_status"] = "not_required"
        scheduler_update["migration_checked_at"] = utc_timestamp()
        message = "Migration request applied; no migration is required for the current schema version."
    elif action in {"cancel_run", "cancel_background_job", "run_final_verifier"}:
        scheduler_update[f"last_{action}_request_id"] = request_id
        message = f"{action} control request recorded for a later execution phase."
    elif action == "rebuild_read_models":
        from runtime.read_models import rebuild_read_models

        payload = request.get("payload") if isinstance(request.get("payload"), Mapping) else {}
        max_dashboard_events = _positive_config_int(payload.get("max_dashboard_events")) if isinstance(payload, Mapping) else None
        rebuild_result = rebuild_read_models(
            paths.project_root,
            write=True,
            workflow_id=str(snapshot.get("workflow_id") or ""),
            max_dashboard_events=max_dashboard_events,
        )
        response_status = "applied" if rebuild_result.get("ok") is True else "rejected"
        scheduler_update[f"last_{action}_request_id"] = request_id
        scheduler_update["last_read_model_rebuild_status"] = rebuild_result.get("status")
        message = "Read models rebuilt by control request." if response_status == "applied" else "Read model rebuild failed."
        response_details["read_model_rebuild"] = dict(rebuild_result)
    if clear_resolved_attention:
        cleared_holds = _clear_manual_runner_availability_holds(
            paths,
            workflow_id=str(snapshot.get("workflow_id") or ""),
            request_id=request_id,
            control_type=action,
        )
        if cleared_holds:
            response_details["cleared_runner_availability_holds"] = cleared_holds
    resulting_status = runtime_status or current_status
    response = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "action": action,
        "type": action,
        "status": response_status,
        "handled_at": utc_timestamp(),
        "scheduler_owner": owner,
        "resulting_workflow_status": resulting_status,
        "message": message,
    }
    response.update(response_details)
    if response_status == "applied" and runtime_status in {"running", "paused", "stopped"}:
        try:
            registry_update = mark_workflow_runtime_status(
                paths.project_root,
                str(snapshot.get("workflow_id") or ""),
                status=runtime_status,
                summary={
                    "one_line": f"Workflow {runtime_status} by control request.",
                    "control_request_id": request_id,
                    "control_type": action,
                },
                updated_by=owner,
            )
            response["workflow_registry_update"] = {
                "status": registry_update.get("status"),
                "workflow_id": registry_update.get("workflow_id"),
                "record_status": (
                    registry_update.get("record", {}).get("status")
                    if isinstance(registry_update.get("record"), Mapping)
                    else None
                ),
            }
        except Exception as error:
            response["workflow_registry_update"] = {"status": "error", "error": str(error)}
    _append_jsonl_locked(paths, paths.runtime_dir / CONTROL_RESPONSES_FILENAME, response)
    if clear_resolved_attention:
        # start/resume is also the acknowledgement boundary after an operator
        # or autonomous repair has made a requires-attention condition safe.
        # If the underlying condition is still unsafe, the next scheduler tick
        # deterministically recreates the attention item.  Keeping the old item
        # here makes the dashboard report a resolved incident indefinitely.
        scheduler_update.update(
            {
                "requires_attention_id": None,
                "requires_attention_type": None,
                "active_background_job_id": None,
                "active_background_job_status": None,
                "wake_next_agent_when": None,
            }
        )
    _update_runtime_state(
        paths,
        status=runtime_status,
        scheduler_update=scheduler_update,
        requires_attention=[] if clear_resolved_attention else None,
    )
    return response


def _clear_manual_runner_availability_holds(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    request_id: str,
    control_type: str,
) -> list[dict[str, Any]]:
    """Release acknowledged manual holds at a start/resume boundary.

    A manual availability hold otherwise prevents the success event that would
    clear it, creating a permanent scheduler deadlock.  Start/resume is already
    the explicit acknowledgement boundary for repaired attention conditions.
    Automatic cooldown holds are intentionally left untouched.
    """

    if not workflow_id:
        return []
    cleared_at = utc_timestamp()

    def update(health: dict[str, Any]) -> list[dict[str, Any]]:
        cleared: list[dict[str, Any]] = []
        runners = health.get("runners")
        if not isinstance(runners, dict):
            return cleared
        for runner_id, record in runners.items():
            if not isinstance(record, dict):
                continue
            hold = record.get("availability_hold")
            if not isinstance(hold, Mapping) or hold.get("status") != "active":
                continue
            if hold.get("requires_attention") is not True:
                continue
            released = dict(hold)
            released.update(
                {
                    "status": "cleared",
                    "cleared_at": cleared_at,
                    "cleared_by_control_request_id": request_id,
                    "cleared_by_control_type": control_type,
                }
            )
            record["availability_hold"] = released
            cleared.append({"runner_id": str(runner_id), "hold": _json_safe_object(released)})
        if cleared:
            health["updated_at"] = cleared_at
        return cleared

    cleared_holds = _update_runner_health_locked(paths, workflow_id=workflow_id, update=update)
    for item in cleared_holds:
        append_event(
            paths,
            workflow_id=workflow_id,
            event_type="runner_availability_hold_cleared",
            data={
                "runner_id": item.get("runner_id"),
                "hold": item.get("hold"),
                "cleared_by_control_request_id": request_id,
                "cleared_by_control_type": control_type,
            },
        )
    return cleared_holds


def _load_runner_and_adapter(project: Path, runner_id: str | None) -> tuple[RunnerConfig, Any]:
    runner_config = load_agent_runners(project).runner(runner_id)
    if not runner_config.enabled:
        raise AgentRunnerConfigError(f"runner {runner_config.runner_id!r} is disabled")
    return runner_config, get_adapter(runner_config.adapter)


def _adapter_input_for_prepared_run(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared: PreparedRun,
    runner_config: RunnerConfig,
    built_prompt: Any,
    failure_id: str | None = None,
) -> AdapterInput:
    return AdapterInput.from_runner_config(
        run_id=prepared.run_id,
        workflow_id=prepared.workflow_id,
        runner_config=runner_config,
        prompt_path=prepared.prompt_path,
        prompt_content=built_prompt.content,
        scheduler_run_dir=prepared.scheduler_run_dir,
        role_output_dir=prepared.role_output_dir,
        task_id=prepared.task_id,
        task_evidence_run_dir=prepared.task_evidence_run_dir,
        cwd=str(_resolve_cwd(project, runner_config.cwd)),
        env=_worker_adapter_env(project, paths, prepared, failure_id=failure_id),
        role=prepared.role,
    )


def _append_adapter_completed_event(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    project: Path,
    prepared: PreparedRun,
    runner_config: RunnerConfig,
    adapter_output: AdapterOutput,
    event_type: str,
    owner: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not EMIT_ADAPTER_COMPLETED_EVENTS:
        return None
    data = {
        "owner": owner,
        "task_id": prepared.task_id,
        "runner_id": prepared.runner_id,
        "adapter": runner_config.adapter,
        "exit_code": adapter_output.exit_code,
        "timed_out": adapter_output.timed_out,
        "adapter_result_path": _path_for_record(project, adapter_output.adapter_result_path),
    }
    if extra:
        data.update(dict(extra))
    return append_event(
        paths,
        workflow_id=workflow_id,
        run_id=prepared.run_id,
        event_type=event_type,
        data=data,
    )


def _execute_worker_action(
    snapshot: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    owner: str,
    lease_heartbeat_interval_seconds: float,
    prepared_role: str,
    scheduler_action: str,
) -> dict[str, Any]:
    paths = snapshot["paths"]
    assert isinstance(paths, WorkflowPaths)
    project = Path(str(snapshot.get("project_root") or paths.project_root)).expanduser().resolve()
    workflow_id = str(snapshot.get("workflow_id") or "unknown_workflow")
    selected = action.get("selected", {})
    selected_mapping = selected if isinstance(selected, Mapping) else {}
    task_id = str(selected_mapping.get("task_id") or "").strip()
    runner_id = str(selected_mapping.get("runner_id") or "").strip() or None
    failure_id = str(selected_mapping.get("failure_id") or "").strip() or None
    started_at = utc_timestamp()
    role_label = "Recovery worker" if prepared_role == "recovery_worker" else "Worker"

    if not task_id:
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="failed_system",
            message=f"{role_label} run selection did not include a task_id.",
            error_code="task_id_missing",
        )
        append_event(paths, workflow_id=workflow_id, event_type=f"{prepared_role}_run_preflight_failed", data=result)
        return result

    try:
        runner_config, adapter = _load_runner_and_adapter(project, runner_id)
    except (AgentRunnerConfigError, AdapterLookupError, OSError, json.JSONDecodeError) as error:
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="waiting_config",
            message=f"{role_label} runner configuration is not usable: {error}",
            error_code=f"{prepared_role}_runner_unavailable",
            task_id=task_id,
            runner_id=runner_id,
        )
        _update_runtime_state(
            paths,
            status="waiting_config",
            scheduler_update={"last_action": scheduler_action, "owner": owner, "selected": selected_mapping},
        )
        append_event(paths, workflow_id=workflow_id, event_type=f"{prepared_role}_run_preflight_failed", data=result)
        return result

    prepared: PreparedRun | None = None
    try:
        prepared = prepare_run(
            project,
            role=prepared_role,
            task_id=task_id,
            runner_id=runner_config.runner_id,
            scheduler_owner=owner,
        )
        built_prompt = build_prompt_for_prepared_run(project, prepared, failure_id=failure_id)
    except (SchedulerError, PromptBuildError, OSError) as error:
        _mark_prepared_run_lease_failed(prepared, owner=owner)
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="failed_system",
            message=f"{role_label} run could not be prepared: {error}",
            error_code=f"{prepared_role}_prepare_or_prompt_failed",
            task_id=task_id,
            runner_id=runner_config.runner_id,
        )
        if prepared_role == "worker":
            failure = _record_worker_failure(
                paths=paths,
                workflow_id=workflow_id,
                project=project,
                result=result,
                task_max_recovery_attempts=_task_max_attempts(snapshot, task_id),
            )
            result["failure_id"] = failure["failure_id"]
            result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
        _update_runtime_state(
            paths,
            status=f"{prepared_role}_run_failed",
            scheduler_update={"last_action": scheduler_action, "owner": owner, "selected": selected_mapping},
        )
        append_event(paths, workflow_id=workflow_id, event_type=f"{prepared_role}_run_preflight_failed", data=result)
        return result

    if prepared_role == "recovery_worker" and failure_id:
        _mark_failure_recovering(
            paths=paths,
            workflow_id=workflow_id,
            project=project,
            failure_id=failure_id,
            prepared=prepared,
        )

    adapter_input = _adapter_input_for_prepared_run(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        built_prompt=built_prompt,
        failure_id=failure_id,
    )
    adapter_input.write_json(prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME)
    run_kind = "recovery" if prepared_role == "recovery_worker" else "worker"
    pre_git_metadata = _capture_worker_run_git_metadata(
        project,
        paths,
        prepared.role_output_dir,
        workflow_id=workflow_id,
        stage="pre",
        task_id=prepared.task_id or task_id,
        run_id=prepared.run_id,
        run_kind=run_kind,
    )

    append_event(
        paths,
        workflow_id=workflow_id,
        run_id=prepared.run_id,
        event_type=f"{prepared_role}_adapter_started",
        data={
            "owner": owner,
            "task_id": prepared.task_id,
            "runner_id": prepared.runner_id,
            "adapter": runner_config.adapter,
            "prompt_path": _path_for_record(project, prepared.prompt_path),
            "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
        },
    )

    try:
        adapter_output = _run_adapter_with_active_run_lease(
            adapter,
            adapter_input,
            prepared,
            owner=owner,
            heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
        )
    except (AdapterContractError, NotImplementedError, RuntimeError) as error:
        adapter_output = _write_adapter_exception_result(adapter_input, error)
        post_git_metadata = _capture_worker_run_git_metadata(
            project,
            paths,
            prepared.role_output_dir,
            workflow_id=workflow_id,
            stage="post",
            task_id=prepared.task_id or task_id,
            run_id=prepared.run_id,
            run_kind=run_kind,
        )
        _refresh_active_run_lease(prepared, status="failed", owner=owner, adapter_pid=os.getpid())
        result = _finish_worker_execution(
            project=project,
            paths=paths,
            prepared=prepared,
            runner_config=runner_config,
            adapter_output=adapter_output,
            owner=owner,
            started_at=started_at,
            classification="adapter_error",
            status="failed_system",
            message=f"{role_label} adapter failed before producing a usable result: {error}",
            worker_provided_agent_status=False,
            scheduler_action=scheduler_action,
            selected_failure_id=failure_id,
            agent_status_record=None,
            boundary_policy=None,
            git_metadata={"pre": pre_git_metadata, "post": post_git_metadata},
            task_max_recovery_attempts=_task_max_attempts(snapshot, task_id),
        )
        append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type=f"{prepared_role}_adapter_failed", data=result)
        return result

    post_git_metadata = _capture_worker_run_git_metadata(
        project,
        paths,
        prepared.role_output_dir,
        workflow_id=workflow_id,
        stage="post",
        task_id=prepared.task_id or task_id,
        run_id=prepared.run_id,
        run_kind=run_kind,
    )

    _append_adapter_completed_event(
        paths,
        workflow_id=workflow_id,
        project=project,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        event_type=f"{prepared_role}_adapter_completed",
        owner=owner,
    )

    agent_status_path = prepared.role_output_dir / "agent_status.json"
    status_record, status_problem = _read_agent_status(agent_status_path)
    boundary_policy: Mapping[str, Any] | None = None
    agent_status_problem: str | None = None
    stderr_excerpt: str | None = None
    if status_problem is not None:
        agent_status_problem = status_problem
        classification = _classify_agent_status_problem(
            status_problem,
            adapter_exit_code=adapter_output.exit_code,
            adapter_timed_out=adapter_output.timed_out,
        )
        status = "failed_agent"
        stderr_excerpt = _short_file_excerpt(adapter_output.stderr_path)
        if adapter_output.exit_code != 0:
            message = (
                f"{role_label} adapter exited with code {adapter_output.exit_code}; "
                f"agent_status.json was {_agent_status_problem_label(status_problem)}."
            )
        elif adapter_output.timed_out:
            message = f"{role_label} adapter timed out; agent_status.json was {_agent_status_problem_label(status_problem)}."
        elif classification == "missing_agent_status":
            message = f"{role_label} adapter exited 0 but did not write agent_status.json."
        else:
            message = f"{role_label} run wrote an invalid agent_status.json: {status_problem}."
        if stderr_excerpt:
            message = f"{message} stderr: {stderr_excerpt}"
        _write_synthetic_failed_agent_status(
            project=project,
            prepared=prepared,
            started_at=started_at,
            ended_at=utc_timestamp(),
            classification=classification,
            message=message,
            adapter_output=adapter_output,
            agent_status_problem=agent_status_problem,
            stderr_excerpt=stderr_excerpt,
        )
        worker_provided_agent_status = False
    elif adapter_output.exit_code != 0:
        boundary_policy = evaluate_worker_write_boundary(
            project,
            paths,
            task_id=prepared.task_id,
            run_dir=prepared.role_output_dir,
            agent_status=status_record,
            adapter_result=adapter_output.to_dict(),
        )
        if not boundary_policy.get("ok"):
            classification = "worker_boundary_violation"
            status = "failed_agent"
            message = worker_write_boundary_message(boundary_policy)
        else:
            classification = "adapter_exit_nonzero"
            status = "failed_agent"
            message = f"{role_label} adapter exited with code {adapter_output.exit_code}."
        worker_provided_agent_status = True
    else:
        boundary_policy = evaluate_worker_write_boundary(
            project,
            paths,
            task_id=prepared.task_id,
            run_dir=prepared.role_output_dir,
            agent_status=status_record,
            adapter_result=adapter_output.to_dict(),
        )
        if not boundary_policy.get("ok"):
            status = "failed_agent"
            classification = "worker_boundary_violation"
            message = worker_write_boundary_message(boundary_policy)
        else:
            status = normalize_worker_status(status_record.get("status")) or "completed"
            classification = "worker_agent_status"
            message = f"{role_label} adapter completed and agent_status.json is available."
        worker_provided_agent_status = True

    final_lease_status = "failed" if status in {"failed_agent", "failed_system"} else "completed"
    _refresh_active_run_lease(prepared, status=final_lease_status, owner=owner, adapter_pid=os.getpid())
    result = _finish_worker_execution(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        owner=owner,
        started_at=started_at,
        classification=classification,
        status=status,
        message=message,
        worker_provided_agent_status=worker_provided_agent_status,
        scheduler_action=scheduler_action,
        selected_failure_id=failure_id,
        agent_status_record=status_record,
        boundary_policy=boundary_policy,
        git_metadata={"pre": pre_git_metadata, "post": post_git_metadata},
        task_max_recovery_attempts=_task_max_attempts(snapshot, task_id),
        agent_status_problem=agent_status_problem,
        stderr_excerpt=stderr_excerpt,
    )
    append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type=f"{prepared_role}_run_classified", data=result)
    return result


def _execute_expansion_action(
    snapshot: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    owner: str,
    lease_heartbeat_interval_seconds: float,
) -> dict[str, Any]:
    paths = snapshot["paths"]
    assert isinstance(paths, WorkflowPaths)
    project = Path(str(snapshot.get("project_root") or paths.project_root)).expanduser().resolve()
    workflow_id = str(snapshot.get("workflow_id") or "unknown_workflow")
    selected = action.get("selected", {}) if isinstance(action.get("selected"), Mapping) else {}
    runner_id = str(selected.get("runner_id") or "").strip() or None
    started_at = utc_timestamp()

    try:
        runner_config, adapter = _load_runner_and_adapter(project, runner_id)
    except (AgentRunnerConfigError, AdapterLookupError, OSError, json.JSONDecodeError) as error:
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="waiting_config",
            message=f"Self-expansion planner runner configuration is not usable: {error}",
            error_code="expansion_planner_runner_unavailable",
            runner_id=runner_id,
        )
        _update_runtime_state(
            paths,
            status="waiting_config",
            scheduler_update={"last_action": "run_expansion_planner", "owner": owner, "selected": selected},
        )
        append_event(paths, workflow_id=workflow_id, event_type="expansion_planner_preflight_failed", data=result)
        return result

    prepared: PreparedRun | None = None
    try:
        prepared = prepare_run(
            project,
            role="expansion_planner",
            task_id=None,
            runner_id=runner_config.runner_id,
            scheduler_owner=owner,
        )
        from runtime.self_expansion import EXPANSION_SELECTION_FILENAME

        selection = {
            **dict(selected),
            "action": "run_expansion_planner",
            "workflow_id": workflow_id,
            "run_id": prepared.run_id,
            "runner_id": runner_config.runner_id,
            "selected_at": utc_timestamp(),
        }
        _write_json(prepared.role_output_dir / EXPANSION_SELECTION_FILENAME, selection)
        _write_json(prepared.scheduler_run_dir / EXPANSION_SELECTION_FILENAME, selection)
        built_prompt = build_prompt_for_prepared_run(project, prepared)
    except (SchedulerError, PromptBuildError, OSError) as error:
        _mark_prepared_run_lease_failed(prepared, owner=owner)
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="failed_system",
            message=f"Self-expansion planner run could not be prepared: {error}",
            error_code="expansion_planner_prepare_or_prompt_failed",
            runner_id=runner_config.runner_id,
        )
        _update_runtime_state(
            paths,
            status="expansion_planner_run_failed",
            scheduler_update={"last_action": "run_expansion_planner", "owner": owner, "selected": selected},
        )
        append_event(paths, workflow_id=workflow_id, event_type="expansion_planner_preflight_failed", data=result)
        return result

    adapter_input = _adapter_input_for_prepared_run(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        built_prompt=built_prompt,
    )
    adapter_input.write_json(prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME)
    append_event(
        paths,
        workflow_id=workflow_id,
        run_id=prepared.run_id,
        event_type="expansion_planner_adapter_started",
        data={
            "owner": owner,
            "runner_id": prepared.runner_id,
            "adapter": runner_config.adapter,
            "prompt_path": _path_for_record(project, prepared.prompt_path),
            "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
            "candidate": selected.get("candidate"),
        },
    )

    try:
        adapter_output = _run_adapter_with_active_run_lease(
            adapter,
            adapter_input,
            prepared,
            owner=owner,
            heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
        )
    except (AdapterContractError, NotImplementedError, RuntimeError) as error:
        adapter_output = _write_adapter_exception_result(adapter_input, error)
        _refresh_active_run_lease(prepared, status="failed", owner=owner, adapter_pid=os.getpid())
        result = _finish_expansion_execution(
            project=project,
            paths=paths,
            prepared=prepared,
            runner_config=runner_config,
            adapter_output=adapter_output,
            owner=owner,
            started_at=started_at,
            classification="adapter_error",
            status="failed_system",
            message=f"Self-expansion planner adapter failed before producing a usable result: {error}",
            agent_status_record=None,
            agent_status_problem="adapter_error",
            selected=selected,
        )
        append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type="expansion_planner_adapter_failed", data=result)
        return result

    _append_adapter_completed_event(
        paths,
        workflow_id=workflow_id,
        project=project,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        event_type="expansion_planner_adapter_completed",
        owner=owner,
    )
    agent_status_path = prepared.role_output_dir / "agent_status.json"
    status_record, status_problem = _read_agent_status(agent_status_path)
    if status_problem is not None:
        status = "failed_agent"
        classification = "missing_agent_status" if status_problem == "missing" else "malformed_agent_status"
        message = f"Self-expansion planner did not write a usable agent_status.json: {status_problem}."
    elif adapter_output.exit_code != 0 or adapter_output.timed_out:
        status = "failed_agent"
        classification = "adapter_exit_nonzero" if adapter_output.exit_code != 0 else "adapter_timed_out"
        message = f"Self-expansion planner adapter exited with code {adapter_output.exit_code}."
    else:
        status = normalize_worker_status(status_record.get("status")) or "completed"
        classification = "expansion_agent_status"
        message = "Self-expansion planner adapter completed and agent_status.json is available."
    final_lease_status = "completed" if is_success_worker_status(status) else "failed"
    _refresh_active_run_lease(prepared, status=final_lease_status, owner=owner, adapter_pid=os.getpid())
    result = _finish_expansion_execution(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        owner=owner,
        started_at=started_at,
        classification=classification,
        status=status,
        message=message,
        agent_status_record=status_record,
        agent_status_problem=status_problem,
        selected=selected,
    )
    append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type="expansion_planner_run_classified", data=result)
    return result


def _execute_objective_verifier_action(
    snapshot: Mapping[str, Any],
    action: Mapping[str, Any],
    *,
    owner: str,
    lease_heartbeat_interval_seconds: float,
) -> dict[str, Any]:
    paths = snapshot["paths"]
    assert isinstance(paths, WorkflowPaths)
    project = Path(str(snapshot.get("project_root") or paths.project_root)).expanduser().resolve()
    workflow_id = str(snapshot.get("workflow_id") or "unknown_workflow")
    selected = action.get("selected", {}) if isinstance(action.get("selected"), Mapping) else {}
    runner_id = str(selected.get("runner_id") or "").strip() or None
    action_name = str(action.get("action") or "run_phase_objective_verifier")
    started_at = utc_timestamp()

    try:
        runner_config, adapter = _load_runner_and_adapter(project, runner_id)
    except (AgentRunnerConfigError, AdapterLookupError, OSError, json.JSONDecodeError) as error:
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="waiting_config",
            message=f"Objective verifier runner configuration is not usable: {error}",
            error_code="objective_verifier_runner_unavailable",
            runner_id=runner_id,
        )
        _update_runtime_state(
            paths,
            status="waiting_config",
            scheduler_update={"last_action": action_name, "owner": owner, "selected": selected},
        )
        append_event(paths, workflow_id=workflow_id, event_type="objective_verifier_preflight_failed", data=result)
        return result

    prepared: PreparedRun | None = None
    try:
        prepared = prepare_run(
            project,
            role="objective_verifier",
            task_id=None,
            runner_id=runner_config.runner_id,
            scheduler_owner=owner,
        )
        from runtime.objective_verification import OBJECTIVE_SELECTION_FILENAME

        selection = {
            **dict(selected),
            "action": action_name,
            "workflow_id": workflow_id,
            "run_id": prepared.run_id,
            "runner_id": runner_config.runner_id,
            "selected_at": utc_timestamp(),
        }
        _write_json(prepared.role_output_dir / OBJECTIVE_SELECTION_FILENAME, selection)
        _write_json(prepared.scheduler_run_dir / OBJECTIVE_SELECTION_FILENAME, selection)
        built_prompt = build_prompt_for_prepared_run(project, prepared)
    except (SchedulerError, PromptBuildError, OSError) as error:
        _mark_prepared_run_lease_failed(prepared, owner=owner)
        result = _worker_preflight_failure(
            project=project,
            paths=paths,
            workflow_id=workflow_id,
            owner=owner,
            started_at=started_at,
            status="failed_system",
            message=f"Objective verifier run could not be prepared: {error}",
            error_code="objective_verifier_prepare_or_prompt_failed",
            runner_id=runner_config.runner_id,
        )
        _update_runtime_state(
            paths,
            status="objective_verifier_run_failed",
            scheduler_update={"last_action": action_name, "owner": owner, "selected": selected},
        )
        append_event(paths, workflow_id=workflow_id, event_type="objective_verifier_preflight_failed", data=result)
        return result

    adapter_input = _adapter_input_for_prepared_run(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        built_prompt=built_prompt,
    )
    adapter_input.write_json(prepared.scheduler_run_dir / ADAPTER_INPUT_FILENAME)
    append_event(
        paths,
        workflow_id=workflow_id,
        run_id=prepared.run_id,
        event_type="objective_verifier_adapter_started",
        data={
            "owner": owner,
            "runner_id": prepared.runner_id,
            "adapter": runner_config.adapter,
            "prompt_path": _path_for_record(project, prepared.prompt_path),
            "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
            "scope": selected.get("scope"),
            "phase_id": selected.get("phase_id"),
            "target_objective_ids": list(selected.get("target_objective_ids") or []),
        },
    )

    try:
        adapter_output = _run_adapter_with_active_run_lease(
            adapter,
            adapter_input,
            prepared,
            owner=owner,
            heartbeat_interval_seconds=lease_heartbeat_interval_seconds,
        )
    except (AdapterContractError, NotImplementedError, RuntimeError) as error:
        adapter_output = _write_adapter_exception_result(adapter_input, error)
        _refresh_active_run_lease(prepared, status="failed", owner=owner, adapter_pid=os.getpid())
        result = _finish_objective_verifier_execution(
            project=project,
            paths=paths,
            prepared=prepared,
            runner_config=runner_config,
            adapter_output=adapter_output,
            owner=owner,
            started_at=started_at,
            classification="adapter_error",
            status="failed_system",
            message=f"Objective verifier adapter failed before producing a usable result: {error}",
            agent_status_record=None,
            agent_status_problem="adapter_error",
            selected=selected,
            scheduler_action=action_name,
        )
        append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type="objective_verifier_adapter_failed", data=result)
        return result

    _append_adapter_completed_event(
        paths,
        workflow_id=workflow_id,
        project=project,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        event_type="objective_verifier_adapter_completed",
        owner=owner,
    )
    agent_status_path = prepared.role_output_dir / "agent_status.json"
    status_record, status_problem = _read_agent_status(agent_status_path)
    if status_problem is not None:
        status = "failed_agent"
        classification = "missing_agent_status" if status_problem == "missing" else "malformed_agent_status"
        message = f"Objective verifier did not write a usable agent_status.json: {status_problem}."
    elif adapter_output.exit_code != 0 or adapter_output.timed_out:
        status = "failed_agent"
        classification = "adapter_exit_nonzero" if adapter_output.exit_code != 0 else "adapter_timed_out"
        message = f"Objective verifier adapter exited with code {adapter_output.exit_code}."
    else:
        status = normalize_worker_status(status_record.get("status")) or "completed"
        classification = "objective_verifier_agent_status"
        message = "Objective verifier adapter completed and agent_status.json is available."
    final_lease_status = "completed" if is_success_worker_status(status) else "failed"
    _refresh_active_run_lease(prepared, status=final_lease_status, owner=owner, adapter_pid=os.getpid())
    result = _finish_objective_verifier_execution(
        project=project,
        paths=paths,
        prepared=prepared,
        runner_config=runner_config,
        adapter_output=adapter_output,
        owner=owner,
        started_at=started_at,
        classification=classification,
        status=status,
        message=message,
        agent_status_record=status_record,
        agent_status_problem=status_problem,
        selected=selected,
        scheduler_action=action_name,
    )
    append_event(paths, workflow_id=workflow_id, run_id=prepared.run_id, event_type="objective_verifier_run_classified", data=result)
    return result


def _finish_objective_verifier_execution(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared: PreparedRun,
    runner_config: RunnerConfig,
    adapter_output: AdapterOutput,
    owner: str,
    started_at: str,
    classification: str,
    status: str,
    message: str,
    agent_status_record: Mapping[str, Any] | None,
    agent_status_problem: str | None,
    selected: Mapping[str, Any],
    scheduler_action: str,
) -> dict[str, Any]:
    from runtime.objective_verification import apply_objective_verification_report

    ended_at = utc_timestamp()
    lease = _read_json_object(prepared.active_run_lease_path, default={})
    agent_status_path = prepared.role_output_dir / "agent_status.json"
    report_path = _project_relative_path(project, selected.get("objective_verification_report"))
    if report_path is None:
        report_path = prepared.role_output_dir / "objective_verification.json"
    ok_agent = is_success_worker_status(status)
    apply_result: Mapping[str, Any] | None = None
    if ok_agent:
        apply_result = apply_objective_verification_report(project, report_path, owner=owner, write=True)
    result_status = str(apply_result.get("status") or status) if isinstance(apply_result, Mapping) else status
    result_ok = bool(apply_result.get("ok")) if isinstance(apply_result, Mapping) else ok_agent
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": result_ok,
        "status": result_status,
        "classification": classification,
        "message": message,
        "project_root": project.as_posix(),
        "workflow_id": prepared.workflow_id,
        "run_id": prepared.run_id,
        "node_id": prepared.node_id,
        "task_id": None,
        "role": prepared.role,
        "runner_id": prepared.runner_id,
        "adapter": runner_config.adapter,
        "adapter_exit_code": adapter_output.exit_code,
        "adapter_timed_out": adapter_output.timed_out,
        "agent_status_path": _path_for_record(project, agent_status_path),
        "agent_status_exists": agent_status_path.is_file(),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "run_dir": _path_for_record(project, prepared.role_output_dir),
        "task_evidence_run_dir": None,
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "stdout_path": _path_for_record(project, prepared.stdout_path),
        "stderr_path": _path_for_record(project, prepared.stderr_path),
        "final_output_path": _path_for_record(project, prepared.final_output_path),
        "adapter_result_path": _path_for_record(project, prepared.adapter_result_path),
        "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
        "active_run_lease_status": lease.get("status"),
        "active_run_lease_heartbeat_count": lease.get("heartbeat_count"),
        "objective_scope": selected.get("scope"),
        "objective_phase_id": selected.get("phase_id"),
        "target_objective_ids": list(selected.get("target_objective_ids") or []),
        "objective_verification_report": _path_for_record(project, report_path),
        "scheduler_owner": owner,
        "started_at": started_at,
        "ended_at": ended_at,
        "selected_objective_gate": _json_safe_object(selected.get("objective_report") or selected),
    }
    if agent_status_problem:
        result["agent_status_problem"] = agent_status_problem
    if isinstance(agent_status_record, Mapping):
        result["agent_status"] = _json_safe_object(agent_status_record)
    if isinstance(apply_result, Mapping):
        result["objective_report_apply"] = dict(apply_result)
        result["errors"] = list(apply_result.get("errors", []))
    runner_availability = _adapter_runner_availability(adapter_output)
    if runner_availability is not None:
        result["runner_availability"] = runner_availability
    # Bounded auto-retry for transient objective-verifier RUN failures. A failed run
    # (agent crashed / API error / no usable report) is registered as a recoverable
    # failure so the loop re-selects the verifier action within budget. A successful
    # run that returns an "unmet" verdict has result_ok=True and is NOT retried here;
    # it flows to self-expansion as normal. On success we resolve any open entry.
    scope_key = _action_failure_scope_key("objective_verifier_failed", result)
    if (
        not result_ok
        and runner_availability is None
        and str(result.get("status") or "") in TRANSIENT_RUN_FAILURE_STATUSES
    ):
        failure_registry_update = _record_action_failure(
            paths=paths,
            workflow_id=prepared.workflow_id,
            project=project,
            result=result,
            failure_class="objective_verifier_failed",
        )
        result["failure_id"] = failure_registry_update["failure_id"]
        result["failure_registry_update"] = dict(failure_registry_update)
        result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
    elif result_ok:
        resolved = _resolve_action_failure(
            paths=paths,
            workflow_id=prepared.workflow_id,
            failure_class="objective_verifier_failed",
            scope_key=scope_key,
            result=result,
        )
        if resolved is not None:
            result["failure_registry_update"] = dict(resolved)
            result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
    runner_health_update = _record_runner_health_event(
        paths=paths,
        workflow_id=prepared.workflow_id,
        result=result,
    )
    if runner_health_update:
        result["runner_health_update"] = dict(runner_health_update)
        result["runner_health_path"] = _path_for_record(project, paths.runtime_dir / RUNNER_HEALTH_FILENAME)
    _write_json(prepared.scheduler_run_dir / RUN_EXECUTION_FILENAME, result)
    _write_json(prepared.role_output_dir / RUN_EXECUTION_FILENAME, result)
    node_summary = _worker_node_summary(result)
    _write_json(prepared.role_output_dir / NODE_SUMMARY_FILENAME, node_summary)
    _write_json(prepared.scheduler_run_dir / NODE_SUMMARY_FILENAME, node_summary)
    # Always leave a human-readable report.md so objective-verifier runs are
    # traceable like worker runs. If the agent wrote one we keep it; otherwise we
    # synthesize a deterministic report from the authoritative JSON verification
    # report (or from the failure/run result when no report exists).
    _ensure_objective_verifier_report_md(
        prepared=prepared,
        project=project,
        report_path=report_path,
        result=result,
    )
    _update_runtime_state(
        paths,
        status="objective_verifier_run_finished" if result_ok else "objective_verifier_run_failed",
        scheduler_update={
            "last_action": scheduler_action,
            "owner": owner,
            "last_run_id": prepared.run_id,
            "last_runner_id": prepared.runner_id,
            "last_objective_scope": selected.get("scope"),
            "last_objective_phase_id": selected.get("phase_id"),
            "last_objective_verification_status": result_status,
            "active_run_id": None,
            "active_node_id": None,
            "active_task_id": None,
        },
    )
    return result


def _ensure_objective_verifier_report_md(
    *,
    prepared: PreparedRun,
    project: Path,
    report_path: Path,
    result: Mapping[str, Any],
) -> None:
    """Guarantee a human-readable report.md exists for an objective-verifier run.

    The agent is asked to write one, but if it does not (or the run failed before
    producing output), we synthesize a concise report from the authoritative JSON
    verification report so every objective gate run is auditable from a single file.
    Best effort: never raise into the run-completion path."""
    try:
        report_md_path = prepared.role_output_dir / "report.md"
        if report_md_path.is_file() and report_md_path.read_text(encoding="utf-8").strip():
            return
        report_json = _read_json_object(report_path, default={})
        lines = _render_objective_verifier_report_md(report_json, result)
        report_md_path.parent.mkdir(parents=True, exist_ok=True)
        report_md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass


def _render_objective_verifier_report_md(
    report_json: Mapping[str, Any], result: Mapping[str, Any]
) -> list[str]:
    scope = str(result.get("objective_scope") or report_json.get("scope") or "objective").strip()
    phase_id = result.get("objective_phase_id") or report_json.get("phase_id")
    overall = str(report_json.get("status") or result.get("status") or "unknown").strip()
    verified_at = str(report_json.get("verified_at") or result.get("ended_at") or "").strip()
    scope_label = f"{scope} objective gate" + (f" ({phase_id})" if phase_id else "")
    lines = [
        f"# Objective verification: {scope_label}",
        "",
        f"- Result: **{overall}**",
    ]
    if verified_at:
        lines.append(f"- Verified at: {verified_at}")
    summary = report_json.get("summary")
    if isinstance(summary, Mapping) and summary:
        parts = [f"{key} {value}" for key, value in summary.items() if value is not None]
        if parts:
            lines.append(f"- Tally: {', '.join(parts)}")
    if not result.get("ok", True):
        lines.append(f"- Run status: {result.get('status')} ({result.get('classification')})")
        if result.get("message"):
            lines.append(f"- Note: {result.get('message')}")
    lines.append("")

    objective_results = report_json.get("objective_results")
    if isinstance(objective_results, Sequence) and not isinstance(objective_results, (str, bytes)):
        for entry in objective_results:
            if not isinstance(entry, Mapping):
                continue
            oid = str(entry.get("objective_id") or "objective").strip()
            ostatus = str(entry.get("status") or "unknown").strip()
            verdict = str(entry.get("verdict") or "").strip()
            confidence = str(entry.get("confidence") or "").strip()
            header = f"## {oid}: {ostatus}"
            if verdict:
                header += f" ({verdict})"
            lines.append(header)
            if confidence:
                lines.append(f"- Confidence: {confidence}")
            rationale = str(entry.get("agent_rationale") or "").strip()
            if rationale:
                lines.append(f"- Judgment: {rationale}")
            gap = str(entry.get("gap_summary") or "").strip()
            if gap:
                lines.append(f"- Gap: {gap}")
            unmet_action = str(entry.get("unmet_action") or "").strip()
            if unmet_action and ostatus != "satisfied":
                lines.append(f"- Unmet action: {unmet_action}")
            lines.append("")
    else:
        lines.append("No per-objective results were recorded in the verification report.")
        lines.append("")
    return lines


def _finish_expansion_execution(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared: PreparedRun,
    runner_config: RunnerConfig,
    adapter_output: AdapterOutput,
    owner: str,
    started_at: str,
    classification: str,
    status: str,
    message: str,
    agent_status_record: Mapping[str, Any] | None,
    agent_status_problem: str | None,
    selected: Mapping[str, Any],
) -> dict[str, Any]:
    from runtime.self_expansion import apply_expansion_proposal

    ended_at = utc_timestamp()
    lease = _read_json_object(prepared.active_run_lease_path, default={})
    agent_status_path = prepared.role_output_dir / "agent_status.json"
    proposal_path = prepared.role_output_dir / EXPANSION_PROPOSAL_FILENAME
    ok_agent = is_success_worker_status(status)
    apply_result: Mapping[str, Any] | None = None
    if ok_agent:
        apply_result = apply_expansion_proposal(
            project,
            proposal_path,
            run_id=prepared.run_id,
            runner_id=prepared.runner_id,
            owner=owner,
        )
    result_status = str(apply_result.get("status") or status) if isinstance(apply_result, Mapping) else status
    result_ok = bool(apply_result.get("ok")) if isinstance(apply_result, Mapping) else ok_agent
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": result_ok,
        "status": result_status,
        "classification": classification,
        "message": message,
        "project_root": project.as_posix(),
        "workflow_id": prepared.workflow_id,
        "run_id": prepared.run_id,
        "node_id": prepared.node_id,
        "task_id": None,
        "role": prepared.role,
        "runner_id": prepared.runner_id,
        "adapter": runner_config.adapter,
        "adapter_exit_code": adapter_output.exit_code,
        "adapter_timed_out": adapter_output.timed_out,
        "agent_status_path": _path_for_record(project, agent_status_path),
        "agent_status_exists": agent_status_path.is_file(),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "run_dir": _path_for_record(project, prepared.role_output_dir),
        "task_evidence_run_dir": None,
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "stdout_path": _path_for_record(project, prepared.stdout_path),
        "stderr_path": _path_for_record(project, prepared.stderr_path),
        "final_output_path": _path_for_record(project, prepared.final_output_path),
        "adapter_result_path": _path_for_record(project, prepared.adapter_result_path),
        "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
        "active_run_lease_status": lease.get("status"),
        "active_run_lease_heartbeat_count": lease.get("heartbeat_count"),
        "proposal_path": _path_for_record(project, proposal_path),
        "scheduler_owner": owner,
        "started_at": started_at,
        "ended_at": ended_at,
        "selected_candidate": _json_safe_object(selected.get("candidate")),
    }
    if agent_status_problem:
        result["agent_status_problem"] = agent_status_problem
    if isinstance(agent_status_record, Mapping):
        result["agent_status"] = _json_safe_object(agent_status_record)
    if isinstance(apply_result, Mapping):
        result["proposal_apply"] = dict(apply_result)
        result["proposal_id"] = apply_result.get("proposal_id")
        result["resolution_strategy"] = apply_result.get("resolution_strategy")
        result["added_task_ids"] = list(apply_result.get("added_task_ids", []))
        result["approval"] = apply_result.get("approval")
        result["errors"] = list(apply_result.get("errors", []))
        result["warnings"] = list(apply_result.get("warnings", []))
    runner_availability = _adapter_runner_availability(adapter_output)
    if runner_availability is not None:
        result["runner_availability"] = runner_availability
    # Bounded auto-retry for transient expansion-planner RUN failures, mirroring the
    # objective-verifier path. Only a failed RUN (agent crashed / no usable proposal)
    # is registered; a successful planner run resolves any open entry.
    scope_key = _action_failure_scope_key("expansion_planner_failed", result)
    if (
        not result_ok
        and runner_availability is None
        and str(result.get("status") or "") in TRANSIENT_RUN_FAILURE_STATUSES
    ):
        failure_registry_update = _record_action_failure(
            paths=paths,
            workflow_id=prepared.workflow_id,
            project=project,
            result=result,
            failure_class="expansion_planner_failed",
        )
        result["failure_id"] = failure_registry_update["failure_id"]
        result["failure_registry_update"] = dict(failure_registry_update)
        result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
    elif result_ok:
        resolved = _resolve_action_failure(
            paths=paths,
            workflow_id=prepared.workflow_id,
            failure_class="expansion_planner_failed",
            scope_key=scope_key,
            result=result,
        )
        if resolved is not None:
            result["failure_registry_update"] = dict(resolved)
            result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
    runner_health_update = _record_runner_health_event(
        paths=paths,
        workflow_id=prepared.workflow_id,
        result=result,
    )
    if runner_health_update:
        result["runner_health_update"] = dict(runner_health_update)
        result["runner_health_path"] = _path_for_record(project, paths.runtime_dir / RUNNER_HEALTH_FILENAME)
    _write_json(prepared.scheduler_run_dir / RUN_EXECUTION_FILENAME, result)
    _write_json(prepared.role_output_dir / RUN_EXECUTION_FILENAME, result)
    node_summary = _worker_node_summary(result)
    _write_json(prepared.role_output_dir / NODE_SUMMARY_FILENAME, node_summary)
    _write_json(prepared.scheduler_run_dir / NODE_SUMMARY_FILENAME, node_summary)
    runtime_status = {
        "applied": "self_expansion_applied",
        "approval_required": "waiting_approval",
        "requires_attention": "requires_attention",
        "validation_failed": "self_expansion_validation_failed",
        "invalid_proposal": "self_expansion_validation_failed",
    }.get(result_status, "expansion_planner_run_finished" if result_ok else "expansion_planner_run_failed")
    _update_runtime_state(
        paths,
        status=runtime_status,
        scheduler_update={
            "last_action": "run_expansion_planner",
            "owner": owner,
            "last_run_id": prepared.run_id,
            "last_runner_id": prepared.runner_id,
            "last_expansion_status": result_status,
            "last_expansion_proposal_id": result.get("proposal_id"),
            "active_run_id": None,
            "active_node_id": None,
            "active_task_id": None,
        },
    )
    return result


def _run_adapter_with_active_run_lease(
    adapter: Any,
    adapter_input: AdapterInput,
    prepared: PreparedRun,
    *,
    owner: str,
    heartbeat_interval_seconds: float,
) -> AdapterOutput:
    _refresh_active_run_lease(prepared, status="running", owner=owner, adapter_pid=os.getpid())
    result: dict[str, Any] = {}

    def run_adapter() -> None:
        try:
            result["output"] = adapter.run(adapter_input)
        except BaseException as error:
            result["error"] = error

    thread = threading.Thread(target=run_adapter, name=f"loopplane-adapter-{prepared.run_id}", daemon=True)
    thread.start()
    interval = min(max(0.01, float(heartbeat_interval_seconds)), 0.5)
    while thread.is_alive():
        thread.join(interval)
        _refresh_active_run_lease(prepared, status="running", owner=owner, adapter_pid=os.getpid())
    error = result.get("error")
    if error is not None:
        raise error
    output = result.get("output")
    if not isinstance(output, AdapterOutput):
        raise AdapterContractError("adapter did not return an AdapterOutput")
    return output


def _capture_worker_run_git_metadata(
    project: Path,
    paths: WorkflowPaths,
    run_dir: Path,
    *,
    workflow_id: str,
    stage: str,
    task_id: str,
    run_id: str,
    run_kind: str,
) -> dict[str, Any]:
    enabled, detail_level = _worker_run_git_metadata_settings(paths)
    if not enabled:
        policy_checkpoint = (
            _maybe_create_worker_policy_checkpoint(project, reason="before_worker_run", task_id=task_id, run_id=run_id)
            if stage == "pre"
            else None
        )
        metadata = {
            "reason": "run_metadata_disabled",
            "config_path": paths.value("version_control_config_file"),
            "detail_level": "none",
        }
        if isinstance(policy_checkpoint, Mapping):
            metadata["policy_checkpoint"] = policy_checkpoint
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_timestamp(),
            "status": "skipped",
            "ok": True,
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "task_id": task_id,
            "run_id": run_id,
            "run_kind": run_kind,
            "stage": stage,
            "run_dir": _path_for_record(project, run_dir),
            "git": None,
            "metadata": metadata,
            "warnings": [],
            "errors": [],
        }
    return capture_run_git_metadata(
        project,
        run_dir,
        stage=stage,
        task_id=task_id,
        run_id=run_id,
        run_kind=run_kind,
        detail_level=detail_level,
    )


def _worker_run_git_metadata_settings(paths: WorkflowPaths) -> tuple[bool, str]:
    config = _read_json_object(paths.version_control_config_file, default={})
    run_metadata = config.get("run_metadata") if isinstance(config, Mapping) else None
    if not isinstance(run_metadata, Mapping):
        return False, DEFAULT_RUN_GIT_METADATA_DETAIL_LEVEL
    enabled = run_metadata.get("enabled") is True
    detail_level = str(run_metadata.get("detail_level") or DEFAULT_RUN_GIT_METADATA_DETAIL_LEVEL).strip().lower()
    if detail_level not in {"full", "status"}:
        detail_level = DEFAULT_RUN_GIT_METADATA_DETAIL_LEVEL
    return enabled, detail_level


def _maybe_create_worker_policy_checkpoint(
    project: Path,
    *,
    reason: str,
    task_id: str,
    run_id: str,
) -> dict[str, Any]:
    key = {
        "before_worker_run": "before_worker_run",
        "after_validation_pass": "after_validation_pass",
    }.get(reason)
    if key is None:
        return {"ok": True, "status": "skipped", "reason": "unsupported_checkpoint_reason"}
    try:
        workflow_config = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "ok": False,
            "status": "skipped",
            "reason": "workflow_config_unavailable",
            "errors": [str(error)],
        }
    config = _read_json_object(paths.version_control_config_file, default={})
    policy = config.get("checkpoint_policy") if isinstance(config, Mapping) else None
    if not isinstance(policy, Mapping) or policy.get(key) is not True:
        return {"ok": True, "status": "skipped", "reason": "checkpoint_policy_disabled"}
    result = create_git_checkpoint(project, reason=reason, task_id=task_id, run_id=run_id)
    compact = {
        "ok": bool(result.get("ok")),
        "status": result.get("status"),
        "reason": reason,
        "warnings": list(result.get("warnings") or []),
        "errors": list(result.get("errors") or []),
    }
    checkpoint = result.get("checkpoint")
    if isinstance(checkpoint, Mapping):
        compact["checkpoint"] = {
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "reason": checkpoint.get("reason"),
            "task_id": checkpoint.get("task_id"),
            "run_id": checkpoint.get("run_id"),
        }
    return compact


def _finish_worker_execution(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared: PreparedRun,
    runner_config: RunnerConfig,
    adapter_output: AdapterOutput,
    owner: str,
    started_at: str,
    classification: str,
    status: str,
    message: str,
    worker_provided_agent_status: bool,
    scheduler_action: str,
    selected_failure_id: str | None,
    agent_status_record: Mapping[str, Any] | None,
    boundary_policy: Mapping[str, Any] | None,
    git_metadata: Mapping[str, Any] | None,
    task_max_recovery_attempts: int,
    agent_status_problem: str | None = None,
    stderr_excerpt: str | None = None,
) -> dict[str, Any]:
    ended_at = utc_timestamp()
    log_copies = _copy_worker_logs(project, prepared)
    lease = _read_json_object(prepared.active_run_lease_path, default={})
    agent_status_path = prepared.role_output_dir / "agent_status.json"
    status = normalize_worker_status(status) or status
    ok = is_success_worker_status(status)
    next_step = "waiting_background" if _agent_status_blocks_next_prompt(status, agent_status_record) else (
        "validation_pending" if ok else "recovery_pending"
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "classification": classification,
        "message": message,
        "project_root": project.as_posix(),
        "workflow_id": prepared.workflow_id,
        "run_id": prepared.run_id,
        "node_id": prepared.node_id,
        "task_id": prepared.task_id,
        "role": prepared.role,
        "runner_id": prepared.runner_id,
        "adapter": runner_config.adapter,
        "adapter_exit_code": adapter_output.exit_code,
        "adapter_timed_out": adapter_output.timed_out,
        "worker_provided_agent_status": worker_provided_agent_status,
        "agent_status_path": _path_for_record(project, agent_status_path),
        "agent_status_exists": agent_status_path.is_file(),
        "scheduler_run_dir": _path_for_record(project, prepared.scheduler_run_dir),
        "role_output_dir": _path_for_record(project, prepared.role_output_dir),
        "run_dir": _path_for_record(project, prepared.task_evidence_run_dir or prepared.role_output_dir),
        "task_evidence_run_dir": (
            _path_for_record(project, prepared.task_evidence_run_dir)
            if prepared.task_evidence_run_dir is not None
            else None
        ),
        "prompt_path": _path_for_record(project, prepared.prompt_path),
        "stdout_path": _path_for_record(project, prepared.stdout_path),
        "stderr_path": _path_for_record(project, prepared.stderr_path),
        "final_output_path": _path_for_record(project, prepared.final_output_path),
        "adapter_result_path": _path_for_record(project, prepared.adapter_result_path),
        "active_run_lease_path": _path_for_record(project, prepared.active_run_lease_path),
        "active_run_lease_status": lease.get("status"),
        "active_run_lease_heartbeat_count": lease.get("heartbeat_count"),
        "log_copies": log_copies,
        "scheduler_owner": owner,
        "started_at": started_at,
        "ended_at": ended_at,
        "next_step": next_step,
    }
    runner_availability = _adapter_runner_availability(adapter_output)
    if runner_availability is not None:
        result["runner_availability"] = runner_availability
        result["next_step"] = "runner_availability_wait"
    if agent_status_problem:
        result["agent_status_problem"] = agent_status_problem
    if stderr_excerpt:
        result["stderr_excerpt"] = stderr_excerpt
    if isinstance(boundary_policy, Mapping):
        result["worker_write_boundary"] = dict(boundary_policy)
    if isinstance(git_metadata, Mapping):
        result["git_metadata"] = dict(git_metadata)
    result["failure_scope"] = _worker_result_failure_scope(result)
    runner_health_update = _record_runner_health_event(
        paths=paths,
        workflow_id=prepared.workflow_id,
        result=result,
    )
    if runner_health_update:
        result["runner_health_update"] = dict(runner_health_update)
        result["runner_health_path"] = _path_for_record(project, paths.runtime_dir / RUNNER_HEALTH_FILENAME)
    background_registry_update = None
    if worker_provided_agent_status and isinstance(agent_status_record, Mapping):
        background_registry_update = _sync_background_jobs_from_agent_status(
            project=project,
            paths=paths,
            prepared=prepared,
            agent_status_record=agent_status_record,
            worker_status=status,
            observed_at=ended_at,
        )
        if background_registry_update is not None:
            result["background_registry_update"] = dict(background_registry_update)
            result["background_registry_path"] = _path_for_record(project, paths.runtime_dir / BACKGROUND_JOBS_FILENAME)
    failure_registry_update: Mapping[str, Any] | None = None
    if prepared.role == "recovery_worker" and selected_failure_id:
        if runner_availability is not None:
            failure_registry_update = _defer_recovery_failure_for_runner_unavailability(
                paths=paths,
                workflow_id=prepared.workflow_id,
                failure_id=selected_failure_id,
                result=result,
            )
        else:
            failure_registry_update = _finish_recovery_failure_update(
                paths=paths,
                workflow_id=prepared.workflow_id,
                project=project,
                failure_id=selected_failure_id,
                result=result,
                agent_status_record=agent_status_record,
            )
        result["failure_id"] = selected_failure_id
        result["failure_registry_update"] = dict(failure_registry_update)
    elif not ok and runner_availability is None:
        failure_registry_update = _record_worker_failure(
            paths=paths,
            workflow_id=prepared.workflow_id,
            project=project,
            result=result,
            task_max_recovery_attempts=task_max_recovery_attempts,
        )
        result["failure_id"] = failure_registry_update["failure_id"]
        result["failure_registry_update"] = dict(failure_registry_update)
        result["failure_registry_path"] = _path_for_record(project, paths.runtime_dir / FAILURE_REGISTRY_FILENAME)
    elif not ok:
        result["failure_registry_update"] = {
            "status": "skipped",
            "reason": "runner_availability_unavailable",
        }
    _write_json(prepared.scheduler_run_dir / RUN_EXECUTION_FILENAME, result)
    _write_json(prepared.role_output_dir / RUN_EXECUTION_FILENAME, result)
    node_summary = _worker_node_summary(result)
    _write_json(prepared.role_output_dir / NODE_SUMMARY_FILENAME, node_summary)
    _write_json(prepared.scheduler_run_dir / NODE_SUMMARY_FILENAME, node_summary)
    if runner_availability is not None:
        runtime_status = "waiting_runner_availability"
    elif next_step == "waiting_background":
        runtime_status = "waiting_background_job"
    else:
        runtime_status = f"{prepared.role}_run_finished" if ok else f"{prepared.role}_run_failed"
    autonomous_background_recovery = bool(
        isinstance(failure_registry_update, Mapping)
        and str(failure_registry_update.get("failure_class") or "")
        == "background_job_failed"
    )
    scheduler_update = {
        "last_action": scheduler_action,
        "owner": owner,
        "last_run_id": prepared.run_id,
        "last_task_id": prepared.task_id,
        "last_runner_id": prepared.runner_id,
        "last_worker_status": status,
        "last_worker_classification": classification,
        "last_next_prompt_ready": _agent_next_prompt_ready(agent_status_record),
        "wake_next_agent_when": _agent_wake_next_agent_when(agent_status_record),
        "active_run_id": None,
        "active_node_id": None,
        "active_task_id": None,
    }
    if autonomous_background_recovery:
        scheduler_update.update(
            {
                "requires_attention_id": None,
                "requires_attention_type": None,
                "active_background_job_id": None,
                "active_background_job_status": None,
            }
        )
    _update_runtime_state(
        paths,
        status=runtime_status,
        scheduler_update=scheduler_update,
        requires_attention=[] if autonomous_background_recovery else None,
    )
    return result


def _worker_preflight_failure(
    *,
    project: Path,
    paths: WorkflowPaths,
    workflow_id: str,
    owner: str,
    started_at: str,
    status: str,
    message: str,
    error_code: str,
    task_id: str | None = None,
    runner_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "classification": error_code,
        "message": message,
        "project_root": project.as_posix(),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "runner_id": runner_id,
        "scheduler_owner": owner,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "runtime_dir": _path_for_record(project, paths.runtime_dir),
    }


def _refresh_active_run_lease(
    prepared: PreparedRun,
    *,
    status: str,
    owner: str,
    adapter_pid: int | None,
) -> dict[str, Any]:
    lease = _read_json_object(prepared.active_run_lease_path, default={})
    if not isinstance(lease, dict):
        lease = {}
    ttl_seconds = _int_value(lease.get("lease_ttl_seconds"), default=ACTIVE_RUN_LEASE_TTL_SECONDS)
    now = utc_timestamp()
    heartbeat_count = _int_value(lease.get("heartbeat_count"), default=0) + 1
    lease.update(
        {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": prepared.workflow_id,
            "run_id": prepared.run_id,
            "node_id": prepared.node_id,
            "task_id": prepared.task_id,
            "role": prepared.role,
            "runner_id": prepared.runner_id,
            "status": status,
            "heartbeat_at": now,
            "lease_expires_at": _timestamp_after(now, ttl_seconds),
            "scheduler_pid": os.getpid(),
            "adapter_pid": adapter_pid,
            "adapter_child_pid": _adapter_child_pid(prepared)
            or _int_value(lease.get("adapter_child_pid"), default=None),
            "scheduler_owner": owner,
            "lease_ttl_seconds": ttl_seconds,
            "heartbeat_count": heartbeat_count,
        }
    )
    _write_active_run_lease(prepared.active_run_lease_path, lease)
    return lease


def _mark_prepared_run_lease_failed(prepared: PreparedRun | None, *, owner: str) -> None:
    if prepared is None:
        return
    try:
        _refresh_active_run_lease(prepared, status="failed", owner=owner, adapter_pid=os.getpid())
    except OSError:
        pass


def _worker_adapter_env(project: Path, paths: WorkflowPaths, prepared: PreparedRun, *, failure_id: str | None = None) -> dict[str, str]:
    env = {
        "LOOPPLANE_PROJECT_ROOT": project.as_posix(),
        "LOOPPLANE_PROJECT_ROOT_REL": ".",
        "LOOPPLANE_PLAN_FILE": paths.plan_file.as_posix(),
        "LOOPPLANE_PLAN_FILE_REL": _path_for_record(project, paths.plan_file),
        "LOOPPLANE_SHARED_CONTEXT_FILE": paths.shared_context_file.as_posix(),
        "LOOPPLANE_SHARED_CONTEXT_FILE_REL": _path_for_record(project, paths.shared_context_file),
        "LOOPPLANE_RESULTS_DIR": paths.results_dir.as_posix(),
        "LOOPPLANE_RESULTS_DIR_REL": _path_for_record(project, paths.results_dir),
        "LOOPPLANE_RUNTIME_DIR": paths.runtime_dir.as_posix(),
        "LOOPPLANE_RUNTIME_DIR_REL": _path_for_record(project, paths.runtime_dir),
        "LOOPPLANE_SCHEDULER_RUN_DIR": prepared.scheduler_run_dir.as_posix(),
        "LOOPPLANE_SCHEDULER_RUN_DIR_REL": _path_for_record(project, prepared.scheduler_run_dir),
        "LOOPPLANE_ROLE_OUTPUT_DIR": prepared.role_output_dir.as_posix(),
        "LOOPPLANE_ROLE_OUTPUT_DIR_REL": _path_for_record(project, prepared.role_output_dir),
        "LOOPPLANE_ACTIVE_RUN_LEASE": prepared.active_run_lease_path.as_posix(),
        "LOOPPLANE_ACTIVE_RUN_LEASE_REL": _path_for_record(project, prepared.active_run_lease_path),
        "LOOPPLANE_ADAPTER_CHILD_PID_FILE": (prepared.scheduler_run_dir / "adapter_child_pid.txt").as_posix(),
    }
    if failure_id:
        env["LOOPPLANE_FAILURE_ID"] = failure_id
    if prepared.task_evidence_run_dir is not None:
        env["LOOPPLANE_TASK_EVIDENCE_RUN_DIR"] = prepared.task_evidence_run_dir.as_posix()
        env["LOOPPLANE_TASK_EVIDENCE_RUN_DIR_REL"] = _path_for_record(project, prepared.task_evidence_run_dir)
    return env


def _adapter_child_pid(prepared: PreparedRun) -> int | None:
    path = prepared.scheduler_run_dir / "adapter_child_pid.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    try:
        pid = int(text.splitlines()[0].strip())
    except (IndexError, TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _write_adapter_exception_result(adapter_input: AdapterInput, error: BaseException) -> AdapterOutput:
    paths = adapter_input.output_paths()
    paths.stdout_path.parent.mkdir(parents=True, exist_ok=True)
    if not paths.stdout_path.exists():
        paths.stdout_path.write_text("", encoding="utf-8")
    paths.stderr_path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
    paths.final_output_path.write_text(f"Worker adapter error: {type(error).__name__}: {error}\n", encoding="utf-8")
    output = AdapterOutput.from_input(
        adapter_input,
        started_at=utc_timestamp(),
        ended_at=utc_timestamp(),
        exit_code=1,
        timed_out=False,
        produced_files=(paths.stdout_path, paths.stderr_path, paths.final_output_path),
        adapter_metadata={"error_type": type(error).__name__, "error": str(error)},
    )
    write_adapter_result(output)
    return output


def _read_agent_status(path: Path) -> tuple[Mapping[str, Any], str | None]:
    if not path.is_file():
        return {}, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {}, f"invalid_json:{error.msg}"
    except OSError as error:
        return {}, f"read_error:{type(error).__name__}"
    if not isinstance(data, Mapping):
        return {}, "not_object"
    if data.get("schema_version") != SCHEMA_VERSION:
        data = _normalized_agent_status_schema(data)
    if not isinstance(data.get("status"), str) or not data.get("status"):
        return data, "status_missing"
    return data, None


def _normalized_agent_status_schema(data: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    original = data.get("schema_version")
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["schema_version_original"] = original
    normalized["schema_version_normalized_by"] = "loopplane_scheduler"
    return normalized


def _classify_agent_status_problem(
    status_problem: str,
    *,
    adapter_exit_code: int,
    adapter_timed_out: bool,
) -> str:
    if adapter_exit_code != 0 or adapter_timed_out:
        return "failed_agent"
    if status_problem == "missing":
        return "missing_agent_status"
    return "malformed_agent_status"


def _agent_status_problem_label(status_problem: str) -> str:
    if status_problem == "missing":
        return "missing"
    return f"invalid ({status_problem})"


def _write_synthetic_failed_agent_status(
    *,
    project: Path,
    prepared: PreparedRun,
    started_at: str,
    ended_at: str,
    classification: str,
    message: str,
    adapter_output: AdapterOutput,
    agent_status_problem: str | None = None,
    stderr_excerpt: str | None = None,
) -> None:
    if prepared.task_evidence_run_dir is None or prepared.task_id is None:
        return
    status_path = prepared.task_evidence_run_dir / "agent_status.json"
    report_path = prepared.task_evidence_run_dir / "report.md"
    commands_path = prepared.task_evidence_run_dir / "commands.sh"
    if not report_path.exists():
        report_path.write_text(f"# Worker Run Classification\n\n{message}\n", encoding="utf-8")
    if not commands_path.exists():
        commands_path.write_text("# Worker did not provide commands.sh before scheduler classification.\n", encoding="utf-8")
    if status_path.exists():
        return
    status = {
        "schema_version": SCHEMA_VERSION,
        "run_id": prepared.run_id,
        "task_id": prepared.task_id,
        "primary_task_id": prepared.task_id,
        "phase": "",
        "status": "failed_agent",
        "next_prompt_ready": True,
        "started_at": started_at,
        "ended_at": ended_at,
        "project_changes": [],
        "commands_run": [],
        "key_outputs": [
            _path_for_record(project, report_path),
            _path_for_record(project, adapter_output.stdout_path),
            _path_for_record(project, adapter_output.stderr_path),
            _path_for_record(project, adapter_output.adapter_result_path),
        ],
        "evidence_satisfies": [],
        "validation_claim": {
            "claim": "failed_agent",
            "checks_claimed": [
                {
                    "name": "agent_status_json",
                    "status": "fail",
                    "message": message,
                }
            ],
            "limitations": [
                "Scheduler-generated status because the worker did not provide a valid agent_status.json."
            ],
        },
        "summary_candidate": {
            "one_line": message,
            "highlights": [],
            "warnings": [classification],
            "blockers": [classification],
        },
        "background": {
            "pids": [],
            "commands": [],
            "logs": [],
            "heartbeat_required": False,
            "wake_next_agent_when": None,
        },
        "repair_attempts": [],
        "known_risks": [message],
        "remaining_incomplete_items": [prepared.task_id],
        "scheduler_classification": {
            "classification": classification,
            "agent_status_problem": agent_status_problem,
            "adapter_exit_code": adapter_output.exit_code,
            "stderr_excerpt": stderr_excerpt,
            "adapter_result_path": _path_for_record(project, adapter_output.adapter_result_path),
        },
    }
    _write_json(status_path, status)


def _short_file_excerpt(path: Path, *, limit: int = 240) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _copy_worker_logs(project: Path, prepared: PreparedRun) -> list[dict[str, str]]:
    if prepared.task_evidence_run_dir is None:
        return []
    log_dir = prepared.task_evidence_run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    copies: list[dict[str, str]] = []
    for source, name in ((prepared.stdout_path, "stdout.log"), (prepared.stderr_path, "stderr.log")):
        if not source.exists():
            continue
        target = log_dir / name
        shutil.copy2(source, target)
        copies.append({"source": _path_for_record(project, source), "path": _path_for_record(project, target)})
    return copies


def _worker_node_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": result.get("workflow_id"),
        "run_id": result.get("run_id"),
        "node_id": result.get("node_id"),
        "task_id": result.get("task_id"),
        "role": result.get("role"),
        "runner_id": result.get("runner_id"),
        "adapter": result.get("adapter"),
        "status": result.get("status"),
        "classification": result.get("classification"),
        "ok": result.get("ok"),
        "message": result.get("message"),
        "adapter_exit_code": result.get("adapter_exit_code"),
        "agent_status_path": result.get("agent_status_path"),
        "run_execution_path": f"{result.get('role_output_dir')}/{RUN_EXECUTION_FILENAME}",
        "updated_at": result.get("ended_at"),
    }


def _agent_next_prompt_ready(agent_status_record: Mapping[str, Any] | None) -> bool | None:
    if not isinstance(agent_status_record, Mapping):
        return None
    value = agent_status_record.get("next_prompt_ready")
    if isinstance(value, bool):
        return value
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        nested = background.get("next_prompt_ready")
        if isinstance(nested, bool):
            return nested
    background_state = agent_status_record.get("background_state")
    if isinstance(background_state, Mapping):
        nested = background_state.get("next_prompt_ready")
        if isinstance(nested, bool):
            return nested
    return None


def _background_record_next_prompt_ready(
    value: object,
    *,
    status: str,
    agent_status_record: Mapping[str, Any] | None,
) -> bool:
    if isinstance(value, bool):
        return value
    normalized_status = str(status or "").strip().lower()
    if normalized_status in BACKGROUND_JOB_SAFE_STATUSES:
        return True
    if normalized_status in {"failed", "timed_out", "stale", "needs_recovery"}:
        return False
    agent_ready = _agent_next_prompt_ready(agent_status_record)
    return bool(agent_ready) if agent_ready is not None else False


def _agent_status_blocks_next_prompt(status: str, agent_status_record: Mapping[str, Any] | None) -> bool:
    return str(status or "").lower() == "running_background"


def _agent_wake_next_agent_when(agent_status_record: Mapping[str, Any] | None) -> str | None:
    if not isinstance(agent_status_record, Mapping):
        return None
    value = agent_status_record.get("wake_next_agent_when")
    if isinstance(value, str) and value.strip():
        return value.strip()
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        nested = background.get("wake_next_agent_when")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    background_state = agent_status_record.get("background_state")
    if isinstance(background_state, Mapping):
        nested = background_state.get("wake_next_agent_when")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _sync_background_jobs_from_agent_status(
    *,
    project: Path,
    paths: WorkflowPaths,
    prepared: PreparedRun,
    agent_status_record: Mapping[str, Any],
    worker_status: str,
    observed_at: str,
) -> dict[str, Any] | None:
    raw_records = _agent_status_background_job_records(agent_status_record)
    blocks_next = _agent_status_blocks_next_prompt(worker_status, agent_status_record)
    if not raw_records and not blocks_next and not _agent_status_has_background_metadata(agent_status_record):
        return None
    if not raw_records:
        raw_records = _background_job_records_for_run(paths, workflow_id=prepared.workflow_id, run_id=prepared.run_id)
        if not raw_records:
            raw_records = [
                _synthetic_background_job_record(
                    prepared=prepared,
                    agent_status_record=agent_status_record,
                    worker_status=worker_status,
                )
            ]

    source_agent_status_path = prepared.role_output_dir / "agent_status.json"
    records = [
        _normalize_background_job_record(
            raw_record,
            project=project,
            prepared=prepared,
            agent_status_record=agent_status_record,
            worker_status=worker_status,
            observed_at=observed_at,
            source_agent_status_path=source_agent_status_path,
            index=index,
        )
        for index, raw_record in enumerate(raw_records)
    ]
    now = _parse_iso_timestamp(observed_at) or datetime.now(UTC)
    evaluated = [_evaluate_background_job_record(record, paths=paths, now=now) for record in records]
    changed: list[dict[str, Any]] = []

    def update(registry: dict[str, Any]) -> None:
        registry["schema_version"] = SCHEMA_VERSION
        registry["workflow_id"] = prepared.workflow_id
        registry["updated_at"] = utc_timestamp()
        jobs = registry.setdefault("jobs", [])
        if not isinstance(jobs, list):
            jobs = []
            registry["jobs"] = jobs
        for record in evaluated:
            existing = _background_job_by_id(jobs, str(record.get("job_id") or ""))
            if existing is None:
                jobs.append(dict(record))
                changed.append(dict(record))
            else:
                existing.update(record)
                changed.append(dict(existing))

    _update_background_registry_locked(paths, workflow_id=prepared.workflow_id, update=update)
    return {
        "status": "updated",
        "jobs_registered": len(changed),
        "job_ids": [str(job.get("job_id")) for job in changed],
        "next_prompt_ready": _agent_next_prompt_ready(agent_status_record),
        "wake_next_agent_when": _agent_wake_next_agent_when(agent_status_record),
    }


def _background_job_records_for_run(paths: WorkflowPaths, *, workflow_id: str, run_id: str | None) -> list[Mapping[str, Any]]:
    run_id_text = _non_empty_text(run_id)
    if run_id_text is None:
        return []
    registry, error = _read_background_job_registry(paths.runtime_dir / BACKGROUND_JOBS_FILENAME, workflow_id=workflow_id)
    if error:
        return []
    records: list[Mapping[str, Any]] = []
    for job in registry.get("jobs", []):
        if not isinstance(job, Mapping):
            continue
        if str(job.get("run_id") or "") == run_id_text:
            records.append(dict(job))
    return records


def _agent_status_background_job_records(agent_status_record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for field in ("background_jobs", "background_job_records"):
        value = agent_status_record.get(field)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            records = [item for item in value if isinstance(item, Mapping)]
            if records:
                return records
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        value = background.get("jobs")
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            records = [item for item in value if isinstance(item, Mapping)]
            if records:
                return records
    background_state = agent_status_record.get("background_state")
    if isinstance(background_state, Mapping):
        for field in ("active_background_jobs", "background_jobs", "jobs"):
            value = background_state.get(field)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                records = [item for item in value if isinstance(item, Mapping)]
                if records:
                    return records
    return []


def _agent_status_has_background_metadata(agent_status_record: Mapping[str, Any]) -> bool:
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        for field in ("pids", "commands", "logs", "wake_next_agent_when"):
            if background.get(field):
                return True
    background_state = agent_status_record.get("background_state")
    if isinstance(background_state, Mapping):
        for field in ("active_background_jobs", "background_jobs", "jobs", "wake_next_agent_when"):
            if background_state.get(field):
                return True
    for field in ("background_pids", "background_commands", "background_logs", "wake_next_agent_when"):
        if agent_status_record.get(field):
            return True
    return False


def _synthetic_background_job_record(
    *,
    prepared: PreparedRun,
    agent_status_record: Mapping[str, Any],
    worker_status: str,
) -> dict[str, Any]:
    pids = _background_list_values(agent_status_record, "pids", "background_pids")
    commands = _background_list_values(agent_status_record, "commands", "background_commands")
    logs = _background_list_values(agent_status_record, "logs", "background_logs")
    record: dict[str, Any] = {
        "job_id": _background_job_id(prepared.task_id, prepared.run_id, 0),
        "task_id": prepared.task_id,
        "run_id": prepared.run_id,
        "status": "running" if _agent_status_blocks_next_prompt(worker_status, agent_status_record) else "pending",
        "wake_next_agent_when": _agent_wake_next_agent_when(agent_status_record),
        "logs": logs,
    }
    if pids:
        record["pid"] = pids[0]
        record["pids"] = pids
    if commands:
        record["command"] = commands[0]
        record["commands"] = commands
    return record


def _normalize_background_job_record(
    raw_record: Mapping[str, Any],
    *,
    project: Path,
    prepared: PreparedRun,
    agent_status_record: Mapping[str, Any],
    worker_status: str,
    observed_at: str,
    source_agent_status_path: Path,
    index: int,
) -> dict[str, Any]:
    record = dict(raw_record)
    task_id = _non_empty_text(record.get("task_id")) or prepared.task_id
    run_id = _non_empty_text(record.get("run_id")) or prepared.run_id
    job_id = _non_empty_text(record.get("job_id")) or _background_job_id(task_id, run_id, index)
    status_value = _non_empty_text(record.get("status")) or (
        "running" if _agent_status_blocks_next_prompt(worker_status, agent_status_record) else "pending"
    )
    status = str(status_value).strip().lower()
    status_problem = None
    if status not in ALLOWED_BACKGROUND_JOB_STATUSES:
        status_problem = f"invalid_status:{status}"
        status = "needs_recovery"

    next_prompt_ready = _background_record_next_prompt_ready(
        record.get("next_prompt_ready"),
        status=status,
        agent_status_record=agent_status_record,
    )

    started_at = _non_empty_text(record.get("started_at")) or _non_empty_text(agent_status_record.get("started_at")) or observed_at
    heartbeat_at = _non_empty_text(record.get("heartbeat_at")) or _non_empty_text(agent_status_record.get("heartbeat_at"))
    if heartbeat_at is None and status in {"pending", "running"}:
        heartbeat_at = observed_at

    wake_next = _non_empty_text(record.get("wake_next_agent_when")) or _agent_wake_next_agent_when(agent_status_record)
    logs = _string_list(record.get("logs")) or _background_list_values(agent_status_record, "logs", "background_logs")
    commands = _string_list(record.get("commands")) or _background_list_values(agent_status_record, "commands", "background_commands")
    command = _non_empty_text(record.get("command")) or (commands[0] if commands else None)

    normalized = dict(record)
    normalized.update(
        {
            "job_id": job_id,
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "next_prompt_ready": next_prompt_ready,
            "started_at": started_at,
            "heartbeat_at": heartbeat_at,
            "wake_next_agent_when": wake_next,
            "logs": logs,
            "source_agent_status_path": _path_for_record(project, source_agent_status_path),
            "updated_at": observed_at,
        }
    )
    if command:
        normalized["command"] = command
        normalized.setdefault("command_hash", "sha256:" + sha256(command.encode("utf-8")).hexdigest())
    if commands:
        normalized["commands"] = commands
    if status_problem:
        normalized["status_problem"] = status_problem
    return normalized


def _background_list_values(agent_status_record: Mapping[str, Any], nested_field: str, top_field: str) -> list[Any]:
    background = agent_status_record.get("background")
    if isinstance(background, Mapping):
        nested = _generic_list(background.get(nested_field))
        if nested:
            return nested
    return _generic_list(agent_status_record.get(top_field))


def _generic_list(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if item is not None and str(item) != ""]
    if value is None or value == "":
        return []
    return [value]


def _string_list(value: object) -> list[str]:
    return [str(item) for item in _generic_list(value)]


def _background_job_id(task_id: str | None, run_id: str, index: int) -> str:
    task = _safe_identifier(task_id or "task")
    suffix = "" if index == 0 else f"_{index + 1}"
    return f"bg_{task}_{_safe_identifier(run_id)}{suffix}"


def _safe_identifier(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in value)
    return cleaned.strip("_") or "unknown"


def _background_job_by_id(jobs: Sequence[Any], job_id: str) -> dict[str, Any] | None:
    for job in jobs:
        if isinstance(job, dict) and str(job.get("job_id") or "") == job_id:
            return job
    return None


def _read_background_job_registry(path: Path, *, workflow_id: str) -> tuple[dict[str, Any], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}, None
    except json.JSONDecodeError as error:
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}, f"invalid_json:{error.msg}"
    except OSError as error:
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}, f"read_error:{type(error).__name__}"
    if isinstance(data, list):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": [item for item in data if isinstance(item, Mapping)]}, None
    if not isinstance(data, Mapping):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}, "not_object_or_array"
    jobs = data.get("jobs", [])
    if not isinstance(jobs, list):
        return {"schema_version": SCHEMA_VERSION, "workflow_id": workflow_id, "jobs": []}, "jobs_not_array"
    registry = dict(data)
    registry["schema_version"] = str(registry.get("schema_version") or SCHEMA_VERSION)
    registry["workflow_id"] = str(registry.get("workflow_id") or workflow_id)
    registry["jobs"] = [item for item in jobs if isinstance(item, Mapping)]
    return registry, None


def _write_background_job_registry(paths: WorkflowPaths, registry: Mapping[str, Any]) -> None:
    _write_json_atomic_fsynced(paths.runtime_dir / BACKGROUND_JOBS_FILENAME, registry)


def _persist_background_jobs_from_snapshot(paths: WorkflowPaths, workflow_id: str, snapshot: Mapping[str, Any]) -> None:
    background_jobs = snapshot.get("background_jobs")
    if not isinstance(background_jobs, Sequence) or isinstance(background_jobs, (str, bytes)):
        return
    snapshot_jobs = [dict(job) for job in background_jobs if isinstance(job, Mapping)]

    def update(registry: dict[str, Any]) -> None:
        current_jobs = [dict(job) for job in registry.get("jobs", []) if isinstance(job, Mapping)]
        current_by_id = {_record_id(job): job for job in current_jobs if _record_id(job)}
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in snapshot_jobs:
            job_id = _record_id(candidate)
            current = current_by_id.get(job_id) if job_id else None
            if current is not None:
                current_updated = _parse_iso_timestamp(current.get("updated_at"))
                candidate_updated = _parse_iso_timestamp(candidate.get("updated_at"))
                if current_updated is not None and (candidate_updated is None or current_updated > candidate_updated):
                    candidate = current
            merged.append(dict(candidate))
            if job_id:
                seen.add(job_id)
        merged.extend(job for job in current_jobs if _record_id(job) not in seen)
        registry.update(
            {
                "schema_version": SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "updated_at": utc_timestamp(),
                "jobs": merged,
            }
        )

    _update_background_registry_locked(paths, workflow_id=workflow_id, update=update)


def _update_background_registry_locked(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    update: Any,
) -> Any:
    owner = f"background-registry:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(
        paths.runtime_dir / "lock" / "background_jobs_lock",
        owner,
        ttl_seconds=BACKGROUND_REGISTRY_LOCK_TTL_SECONDS,
    )
    with lock.acquire(timeout_seconds=BACKGROUND_REGISTRY_LOCK_WAIT_SECONDS):
        registry, error = _read_background_job_registry(paths.runtime_dir / BACKGROUND_JOBS_FILENAME, workflow_id=workflow_id)
        if error:
            registry.setdefault("registry_errors", []).append(error)
        result = update(registry)
        _write_background_job_registry(paths, registry)
        return result


def _evaluate_background_job_record(
    record: Mapping[str, Any],
    *,
    paths: WorkflowPaths,
    now: datetime,
) -> dict[str, Any]:
    job = dict(record)
    status = str(job.get("status") or "").strip().lower()
    if status not in ALLOWED_BACKGROUND_JOB_STATUSES:
        job["status"] = "needs_recovery"
        job["status_problem"] = f"invalid_status:{status or '<missing>'}"
        job["next_prompt_ready"] = False
        return job

    source_status = _background_status_from_source_agent_status(paths, job)
    if source_status is not None:
        return source_status

    if status in BACKGROUND_JOB_SAFE_STATUSES:
        job["next_prompt_ready"] = job.get("next_prompt_ready") is not False
        return job
    if status in {"failed", "timed_out", "stale", "needs_recovery"}:
        job["next_prompt_ready"] = False
        return job

    exit_code_status = _status_from_exit_code_file(paths, job)
    if exit_code_status is not None:
        job["status"] = exit_code_status
        job["next_prompt_ready"] = exit_code_status in BACKGROUND_JOB_SAFE_STATUSES
        return job
    if _job_marker_exists(paths, job.get("done_marker")):
        job["status"] = "completed"
        job["next_prompt_ready"] = True
        return job

    wake_status = _status_from_wake_check(paths, job)
    if wake_status is not None:
        job["status"] = wake_status
        job["next_prompt_ready"] = wake_status in BACKGROUND_JOB_SAFE_STATUSES
        return job

    timeout_at = _parse_iso_timestamp(job.get("timeout_at"))
    if timeout_at is not None and timeout_at < now and status not in BACKGROUND_JOB_TERMINAL_STATUSES:
        job["status"] = "timed_out"
        job["next_prompt_ready"] = False
        return job

    if status in {"pending", "running"}:
        heartbeat = _parse_iso_timestamp(job.get("heartbeat_at") or job.get("started_at"))
        if heartbeat is None:
            job["status"] = "needs_recovery"
            job["status_problem"] = "missing_parseable_heartbeat"
            job["next_prompt_ready"] = False
            return job
        age = max(0, int((now - heartbeat).total_seconds()))
        job["heartbeat_age_seconds"] = age
        if age > BACKGROUND_JOB_HEARTBEAT_TTL_SECONDS:
            child_pid = _positive_int(job.get("pid") or job.get("child_pid"))
            supervisor_pid = _positive_int(job.get("supervisor_pid"))
            if (
                child_pid is not None
                and supervisor_pid is not None
                and _pid_exists(child_pid)
                and _pid_exists(supervisor_pid)
            ):
                # Both independently recorded process levels are live.  This
                # is stronger evidence than a metadata heartbeat during a
                # synchronous watchdog inspection and also covers a short
                # registry-snapshot race around current_check_id.
                job["next_prompt_ready"] = False
                if job.get("status_problem") == "stale_heartbeat":
                    job.pop("status_problem", None)
                return job
            job["status"] = "stale"
            job["status_problem"] = "stale_heartbeat"
            job["next_prompt_ready"] = False
            return job
        pid_value = job.get("pid")
        if pid_value is not None:
            pid = _positive_int(pid_value)
            if pid is None or _pid_exists(pid) is False:
                if pid is not None and _background_pid_missing_is_within_startup_grace(job, pid=pid, age_seconds=age):
                    job["next_prompt_ready"] = False
                    return job
                job["status"] = "stale"
                job["status_problem"] = "process_not_live"
                job["next_prompt_ready"] = False
                return job
        if job.get("status_problem") in {"stale_heartbeat", "process_not_live", "missing_parseable_heartbeat"}:
            job.pop("status_problem", None)
        job["next_prompt_ready"] = False if job.get("next_prompt_ready") is not True else True
    elif status in {"failed", "timed_out", "stale", "needs_recovery"}:
        job["next_prompt_ready"] = False
    return job


def _background_status_from_source_agent_status(paths: WorkflowPaths, job: Mapping[str, Any]) -> dict[str, Any] | None:
    if job.get("manual_resolution") is True:
        return None
    if str(job.get("source") or "") == "loopplane_background_start":
        return None
    status_path = _resolve_job_path(paths, job.get("source_agent_status_path"))
    if status_path is None or not status_path.is_file():
        return None
    status_record = _read_json_object(status_path, default={})
    if not isinstance(status_record, Mapping):
        return None
    run_id = _non_empty_text(job.get("run_id"))
    source_run_id = _non_empty_text(status_record.get("run_id"))
    if run_id is not None and source_run_id is not None and run_id != source_run_id:
        return None
    task_id = _non_empty_text(job.get("task_id"))
    source_task_id = _non_empty_text(status_record.get("task_id") or status_record.get("primary_task_id"))
    if task_id is not None and source_task_id is not None and task_id != source_task_id:
        return None
    worker_status = normalize_worker_status(status_record.get("status")) or str(status_record.get("status") or "").strip().lower()
    if _agent_status_blocks_next_prompt(worker_status, status_record):
        return None
    if _agent_next_prompt_ready(status_record) is True or is_success_worker_status(worker_status):
        updated = dict(job)
        updated["status"] = "completed"
        updated["next_prompt_ready"] = True
        updated["ended_at"] = _non_empty_text(status_record.get("ended_at")) or updated.get("ended_at") or utc_timestamp()
        updated["updated_at"] = utc_timestamp()
        updated["resolved_from_source_agent_status"] = True
        updated.pop("status_problem", None)
        return updated
    return None


def _status_from_exit_code_file(paths: WorkflowPaths, job: Mapping[str, Any]) -> str | None:
    path = _resolve_job_path(paths, job.get("exit_code_file"))
    if path is None or not path.is_file():
        return None
    try:
        exit_code_text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not exit_code_text:
        return None
    try:
        exit_code = int(exit_code_text)
    except ValueError:
        return "needs_recovery"
    return "completed" if exit_code == 0 else "failed"


def _background_pid_missing_is_within_startup_grace(job: Mapping[str, Any], *, pid: int, age_seconds: int) -> bool:
    child_pid = _positive_int(job.get("child_pid"))
    supervisor_pid = _positive_int(job.get("supervisor_pid"))
    if child_pid is not None:
        return False
    if supervisor_pid is None or supervisor_pid != pid:
        return False
    return age_seconds <= BACKGROUND_JOB_PID_STARTUP_GRACE_SECONDS


def _status_from_wake_check(paths: WorkflowPaths, job: Mapping[str, Any]) -> str | None:
    wake_check = job.get("wake_check")
    if not isinstance(wake_check, Mapping):
        return None
    wake_type = str(wake_check.get("type") or "")
    if wake_type not in {"file_exists", "file_exists_and_process_exited"}:
        return None
    raw_paths = wake_check.get("paths")
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        return "needs_recovery"
    resolved = [_resolve_job_path(paths, value) for value in raw_paths]
    if not resolved or not all(path is not None and path.exists() for path in resolved):
        return None
    if wake_type == "file_exists_and_process_exited":
        pid = _positive_int(job.get("pid"))
        if pid is not None and _pid_exists(pid) is True:
            return None
    return _status_from_exit_code_file(paths, job) or "completed"


def _job_marker_exists(paths: WorkflowPaths, value: object) -> bool:
    path = _resolve_job_path(paths, value)
    return path.is_file() if path is not None else False


def _resolve_job_path(paths: WorkflowPaths, value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return paths.project_root / path


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _active_lease_process_liveness(lease: Mapping[str, Any]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for field in ("adapter_child_pid", "adapter_pid", "scheduler_pid"):
        raw_pid = lease.get(field)
        pid = _positive_int(raw_pid)
        if pid is None:
            if raw_pid is not None:
                entries.append({"field": field, "pid": raw_pid, "liveness": "invalid"})
            continue
        alive = _pid_exists(pid)
        entries.append(
            {
                "field": field,
                "pid": pid,
                "liveness": "alive" if alive is True else ("dead" if alive is False else "unavailable"),
            }
        )
    runner_entries = [
        entry
        for entry in entries
        if str(entry.get("field") or "") in {"adapter_child_pid", "adapter_pid"}
    ]
    aggregate_entries = runner_entries or entries
    if any(entry.get("liveness") == "alive" for entry in aggregate_entries):
        liveness = "alive"
        alive: bool | None = True
    elif any(entry.get("liveness") == "unavailable" for entry in aggregate_entries):
        liveness = "unavailable"
        alive = None
    elif aggregate_entries:
        liveness = "dead"
        alive = False
    else:
        liveness = "unavailable"
        alive = None
    return {
        "liveness": liveness,
        "alive": alive,
        "processes": entries,
    }


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


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _non_empty_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _read_failure_registry(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    data = _read_json_object(paths.runtime_dir / FAILURE_REGISTRY_FILENAME, default={})
    failures = data.get("failures") if isinstance(data, Mapping) else []
    if not isinstance(failures, list):
        failures = []
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(data.get("workflow_id") or workflow_id) if isinstance(data, Mapping) else workflow_id,
        "failures": [dict(failure) for failure in failures if isinstance(failure, Mapping)],
    }


def _write_failure_registry(paths: WorkflowPaths, registry: Mapping[str, Any]) -> None:
    _write_json(paths.runtime_dir / FAILURE_REGISTRY_FILENAME, registry)


def _read_runner_health(paths: WorkflowPaths, *, workflow_id: str) -> dict[str, Any]:
    data = _read_json_object(paths.runtime_dir / RUNNER_HEALTH_FILENAME, default={})
    runners = data.get("runners") if isinstance(data, Mapping) else {}
    if not isinstance(runners, Mapping):
        runners = {}
    normalized_runners: dict[str, dict[str, Any]] = {}
    for runner_id, record in runners.items():
        if not isinstance(runner_id, str) or not isinstance(record, Mapping):
            continue
        events = record.get("events", [])
        if not isinstance(events, list):
            events = []
        normalized = {str(key): value for key, value in record.items() if key != "events"}
        normalized["events"] = [dict(event) for event in events if isinstance(event, Mapping)]
        normalized_runners[runner_id] = normalized
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": str(data.get("workflow_id") or workflow_id) if isinstance(data, Mapping) else workflow_id,
        "runners": normalized_runners,
    }


def _write_runner_health(paths: WorkflowPaths, health: Mapping[str, Any]) -> None:
    _write_json(paths.runtime_dir / RUNNER_HEALTH_FILENAME, health)


def _update_runner_health_locked(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    update: Any,
) -> Any:
    owner = f"runner-health:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "runner_health_lock", owner, ttl_seconds=30)
    with lock.acquire():
        health = _read_runner_health(paths, workflow_id=workflow_id)
        result = update(health)
        _write_runner_health(paths, health)
        return result


def _record_runner_health_event(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    runner_id = str(result.get("runner_id") or "")
    if not runner_id:
        return {}
    observed_at = str(result.get("ended_at") or utc_timestamp())
    scope = _runner_health_event_scope(result)
    event = {
        "observed_at": observed_at,
        "scope": scope,
        "failure_scope": result.get("failure_scope"),
        "run_id": result.get("run_id"),
        "task_id": result.get("task_id"),
        "role": result.get("role"),
        "status": result.get("status"),
        "classification": result.get("classification"),
        "adapter": result.get("adapter"),
        "adapter_exit_code": result.get("adapter_exit_code"),
        "adapter_timed_out": result.get("adapter_timed_out"),
        "worker_provided_agent_status": result.get("worker_provided_agent_status"),
    }
    availability = result.get("runner_availability")
    if isinstance(availability, Mapping):
        event["runner_availability"] = _json_safe_object(availability)
    if result.get("agent_status_problem"):
        event["agent_status_problem"] = result.get("agent_status_problem")

    changed: dict[str, Any] = {}
    availability_transition: dict[str, Any] | None = None

    def update(health: dict[str, Any]) -> None:
        nonlocal changed, availability_transition
        health["schema_version"] = SCHEMA_VERSION
        health["workflow_id"] = workflow_id
        health["updated_at"] = observed_at
        runners = health.setdefault("runners", {})
        if not isinstance(runners, dict):
            runners = {}
            health["runners"] = runners
        record = runners.setdefault(runner_id, {"runner_id": runner_id, "events": []})
        if not isinstance(record, dict):
            record = {"runner_id": runner_id, "events": []}
            runners[runner_id] = record
        events = record.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            record["events"] = events
        events.append(event)
        del events[:-RUNNER_HEALTH_EVENT_LIMIT]
        record["runner_id"] = runner_id
        record["last_event_at"] = observed_at
        record["last_scope"] = scope
        if scope == "success":
            record["last_success_at"] = observed_at
            existing_hold = record.get("availability_hold")
            if isinstance(existing_hold, Mapping) and existing_hold.get("status") == "active":
                cleared = dict(existing_hold)
                cleared["status"] = "cleared"
                cleared["cleared_at"] = observed_at
                cleared["cleared_by_run_id"] = result.get("run_id")
                record["availability_hold"] = cleared
                availability_transition = {"transition": "cleared", "hold": cleared}
        elif scope == "runner_failure":
            record["last_runner_failure_at"] = observed_at
        elif scope == "runner_unavailable":
            record["last_runner_unavailable_at"] = observed_at
            hold = _runner_availability_hold_from_result(result, observed_at=observed_at)
            existing_hold = record.get("availability_hold")
            if isinstance(existing_hold, Mapping) and _same_availability_hold(existing_hold, hold):
                updated = dict(existing_hold)
                updated["last_seen_at"] = observed_at
                updated["last_source_run_id"] = result.get("run_id")
                updated["seen_count"] = _int_value(updated.get("seen_count"), default=1) + 1
                if hold.get("cooldown_until"):
                    updated["cooldown_until"] = hold.get("cooldown_until")
                record["availability_hold"] = updated
                availability_transition = {"transition": "updated", "hold": updated}
            else:
                record["availability_hold"] = hold
                availability_transition = {"transition": "started", "hold": hold}
        else:
            record["last_non_runner_event_at"] = observed_at
        changed = {
            "runner_id": runner_id,
            "scope": scope,
            "observed_at": observed_at,
            "event_count": len(events),
        }
        if availability_transition is not None:
            changed["availability_transition"] = availability_transition.get("transition")
            changed["availability_hold"] = _json_safe_object(availability_transition.get("hold"))

    _update_runner_health_locked(paths, workflow_id=workflow_id, update=update)
    if availability_transition and availability_transition.get("transition") in {"started", "cleared"}:
        transition = str(availability_transition.get("transition"))
        append_event(
            paths,
            workflow_id=workflow_id,
            run_id=str(result.get("run_id") or "") or None,
            event_type=f"runner_availability_hold_{transition}",
            data={
                "runner_id": runner_id,
                "role": result.get("role"),
                "adapter": result.get("adapter"),
                "hold": _json_safe_object(availability_transition.get("hold")),
            },
        )
    return changed


def _runner_health_event_scope(result: Mapping[str, Any]) -> str:
    if bool(result.get("ok")):
        return "success"
    if isinstance(result.get("runner_availability"), Mapping):
        return "runner_unavailable"
    failure_scope = str(result.get("failure_scope") or "")
    if failure_scope == "runner":
        return "runner_failure"
    if failure_scope == "policy":
        return "policy_failure"
    return "task_failure"


def _worker_result_failure_scope(result: Mapping[str, Any]) -> str:
    if bool(result.get("ok")):
        return "success"
    if isinstance(result.get("runner_availability"), Mapping):
        return "runner"
    if result.get("adapter_exit_code") == ADAPTER_POLICY_BLOCKED_EXIT_CODE:
        return "policy"
    if result.get("worker_provided_agent_status") is False:
        return "runner"
    if result.get("agent_status_problem"):
        return "runner"
    return "task"


def _adapter_runner_availability(adapter_output: AdapterOutput) -> dict[str, Any] | None:
    metadata = adapter_output.adapter_metadata
    availability = metadata.get("runner_availability") if isinstance(metadata, Mapping) else None
    if not isinstance(availability, Mapping) or availability.get("status") != "unavailable":
        return None
    return _json_safe_object(availability)


def _runner_availability_hold_from_result(result: Mapping[str, Any], *, observed_at: str) -> dict[str, Any]:
    availability = result.get("runner_availability") if isinstance(result.get("runner_availability"), Mapping) else {}
    assert isinstance(availability, Mapping)
    scope = availability.get("scope") if isinstance(availability.get("scope"), Mapping) else {}
    return {
        "status": "active",
        "reason_class": str(availability.get("reason_class") or "unknown_runner_unavailable"),
        "recoverability": str(availability.get("recoverability") or "manual"),
        "scope": _json_safe_object(scope),
        "cooldown_until": availability.get("cooldown_until"),
        "retry_after_seconds": availability.get("retry_after_seconds"),
        "requires_attention": bool(availability.get("requires_attention")),
        "confidence": availability.get("confidence"),
        "fingerprint": availability.get("fingerprint"),
        "first_seen_at": observed_at,
        "last_seen_at": observed_at,
        "seen_count": 1,
        "last_source_run_id": result.get("run_id"),
        "last_task_id": result.get("task_id"),
        "message_excerpt": (
            availability.get("evidence", {}).get("message_excerpt")
            if isinstance(availability.get("evidence"), Mapping)
            else None
        ),
    }


def _same_availability_hold(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        str(left.get("reason_class") or "") == str(right.get("reason_class") or "")
        and str(left.get("fingerprint") or "") == str(right.get("fingerprint") or "")
        and _same_availability_scope(
            left.get("scope") if isinstance(left.get("scope"), Mapping) else {},
            right.get("scope") if isinstance(right.get("scope"), Mapping) else {},
        )
    )


def _update_failure_registry_locked(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    update: Any,
) -> Any:
    owner = f"failure-registry:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "failure_registry_lock", owner, ttl_seconds=30)
    with lock.acquire():
        registry = _read_failure_registry(paths, workflow_id=workflow_id)
        result = update(registry)
        _write_failure_registry(paths, registry)
        return result


def _ingest_failed_validations(paths: WorkflowPaths, *, workflow_id: str) -> list[dict[str, Any]]:
    try:
        validation_files = sorted(path for path in paths.results_dir.glob("**/validation.json") if path.is_file())
    except OSError:
        return []
    candidates: list[dict[str, Any]] = []
    for validation_path in validation_files:
        validation = _read_json_object(validation_path, default={})
        if not isinstance(validation, Mapping):
            continue
        status = str(validation.get("status") or "").lower()
        if status not in {"fail", "failed", "rejected"}:
            continue
        task_id = _validation_task_id(paths, validation_path, validation)
        if not task_id:
            continue
        seen_at = utc_timestamp()
        candidates.append(
            {
                "failure_id": _new_failure_id(),
                "task_id": task_id,
                "run_id": str(validation.get("run_id") or validation_path.parent.name),
                "status": "unrecovered",
                "failure_class": "validation_failed",
                "failure_signature": _validation_failure_signature(validation),
                "summary": str(
                    validation.get("summary")
                    or validation.get("message")
                    or validation.get("reason")
                    or "Validation failed."
                ),
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "attempts": 1,
                "recovery_attempts": 0,
                "max_recovery_attempts": DEFAULT_MAX_RECOVERY_ATTEMPTS,
                "budget_remaining": True,
                "recoverable": True,
                "run_ids": [str(validation.get("run_id") or validation_path.parent.name)],
                "source_validation_path": _path_for_record(paths.project_root, validation_path),
            }
        )

    if not candidates:
        return []

    changed: list[dict[str, Any]] = []

    def update(registry: dict[str, Any]) -> None:
        for candidate in candidates:
            failure = _upsert_failure(registry, candidate)
            if failure is not None:
                changed.append(dict(failure))

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed


def _ingest_failed_background_jobs(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    background_jobs: object,
    tasks: object,
) -> list[dict[str, Any]]:
    """Turn task-associated background failures into autonomous recovery work.

    A failed supervised command is ordinary task execution evidence, not a human
    approval boundary.  Valid task-associated records therefore enter the same
    bounded recovery queue as worker and validation failures.  Malformed records
    without a resolvable task remain conservative ``requires_attention`` cases.
    """

    if not isinstance(background_jobs, Sequence) or isinstance(
        background_jobs, (str, bytes)
    ):
        return []
    task_records = {
        str(task.get("task_id") or ""): task
        for task in tasks
        if isinstance(task, Mapping) and str(task.get("task_id") or "")
    } if isinstance(tasks, Sequence) and not isinstance(tasks, (str, bytes)) else {}
    candidates: list[dict[str, Any]] = []
    seen_at = utc_timestamp()
    for job in background_jobs:
        if not isinstance(job, Mapping):
            continue
        status = str(job.get("status") or "").strip().lower()
        if status not in BACKGROUND_JOB_ATTENTION_STATUSES:
            continue
        task_id = str(job.get("task_id") or "").strip()
        task = task_records.get(task_id)
        job_id = _record_id(job)
        if task is None or not job_id:
            continue
        run_id = str(job.get("run_id") or "")
        exit_code = job.get("exit_code")
        status_problem = str(job.get("status_problem") or "").strip()
        if status == "needs_recovery" and status_problem.startswith("invalid_status:"):
            # An unrecognized producer status is a malformed control record,
            # not a safely classified task failure. Keep the conservative
            # attention path until the record itself can be interpreted.
            continue
        summary = f"Background job {job_id} for {task_id} ended with status {status}"
        if exit_code is not None:
            summary += f" and exit code {exit_code}"
        if status_problem:
            summary += f" ({status_problem})"
        summary += ". Inspect its recorded command and logs, repair the task, run the smallest validation, and resume it idempotently."
        task_budget = _int_value(
            task.get("max_attempts"), default=DEFAULT_MAX_RECOVERY_ATTEMPTS
        )
        candidates.append(
            {
                "failure_id": _new_failure_id(),
                "task_id": task_id,
                "run_id": run_id,
                "status": "unrecovered",
                "failure_class": "background_job_failed",
                "failure_signature": ":".join(
                    (
                        "background_job_failed",
                        status or "unknown",
                        f"exit_{exit_code}",
                        status_problem or "no_status_problem",
                    )
                ),
                "summary": summary,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "attempts": 1,
                "recovery_attempts": 0,
                "max_recovery_attempts": max(
                    DEFAULT_MAX_RECOVERY_ATTEMPTS, task_budget
                ),
                "budget_remaining": True,
                "recoverable": True,
                "run_ids": [run_id] if run_id else [],
                "source_background_job_id": job_id,
                "source_background_status": status,
                "source_background_exit_code": exit_code,
                "source_agent_status_path": job.get("source_agent_status_path"),
                "background_command": job.get("command"),
                "background_command_hash": job.get("command_hash"),
                "background_logs": list(job.get("logs") or []),
                "background_exit_code_file": job.get("exit_code_file"),
            }
        )
    if not candidates:
        return []

    changed: list[dict[str, Any]] = []

    def update(registry: dict[str, Any]) -> None:
        failures = registry.setdefault("failures", [])
        if not isinstance(failures, list):
            failures = []
            registry["failures"] = failures
        known_background_ids = {
            str(failure.get("source_background_job_id") or "")
            for failure in failures
            if isinstance(failure, Mapping)
        }
        for candidate in candidates:
            source_id = str(candidate.get("source_background_job_id") or "")
            if source_id in known_background_ids:
                continue
            record = dict(candidate)
            _refresh_failure_budget(record)
            failures.append(record)
            known_background_ids.add(source_id)
            changed.append(dict(record))

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed


def _record_worker_failure(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    project: Path,
    result: Mapping[str, Any],
    task_max_recovery_attempts: int,
) -> dict[str, Any]:
    seen_at = str(result.get("ended_at") or utc_timestamp())
    run_id = str(result.get("run_id") or "")
    candidate = {
        "failure_id": _new_failure_id(),
        "task_id": str(result.get("task_id") or ""),
        "run_id": run_id,
        "status": "unrecovered",
        "failure_class": "worker_failed",
        "failure_signature": _worker_failure_signature(result),
        "summary": str(result.get("message") or "Worker run failed."),
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "attempts": 1,
        "recovery_attempts": 0,
        "max_recovery_attempts": max(DEFAULT_MAX_RECOVERY_ATTEMPTS, task_max_recovery_attempts),
        "budget_remaining": True,
        "recoverable": True,
        "run_ids": [run_id] if run_id else [],
        "agent_status_path": result.get("agent_status_path"),
        "adapter_result_path": result.get("adapter_result_path"),
        "scheduler_run_dir": result.get("scheduler_run_dir"),
        "role_output_dir": result.get("role_output_dir"),
    }
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        changed = _upsert_failure(registry, candidate)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    if changed is None:
        registry = _read_failure_registry(paths, workflow_id=workflow_id)
        changed = _matching_failure(registry.get("failures", []), candidate) or candidate
    return dict(changed)


def _action_failure_scope_key(failure_class: str, result: Mapping[str, Any]) -> str:
    """Stable identity for a non-task action failure (objective verifier / planner).

    There is no task_id, so we key by the failure class plus the run scope so that
    repeated transient failures of the same gate dedupe/increment one entry."""
    if failure_class == "objective_verifier_failed":
        scope = str(result.get("objective_scope") or "")
        phase_id = str(result.get("objective_phase_id") or "")
        return f"objective_verifier:{scope}:{phase_id}"
    return "expansion_planner"


def _action_failure_signature(failure_class: str, result: Mapping[str, Any]) -> str:
    parts = [
        failure_class,
        str(result.get("classification") or "action_failed"),
        str(result.get("status") or "failed"),
        f"exit_{result.get('adapter_exit_code')}",
        f"timed_out_{str(bool(result.get('adapter_timed_out'))).lower()}",
    ]
    return ":".join(parts)


def _record_action_failure(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    project: Path,
    result: Mapping[str, Any],
    failure_class: str,
) -> dict[str, Any]:
    """Record a transient objective-verifier / expansion-planner run failure as a
    recoverable failure_registry entry, mirroring `_record_worker_failure` but keyed
    by the action scope (task_id is empty) so the SAME action is re-selected on the
    next tick within a bounded recovery budget."""
    seen_at = str(result.get("ended_at") or utc_timestamp())
    run_id = str(result.get("run_id") or "")
    scope_key = _action_failure_scope_key(failure_class, result)
    signature = _action_failure_signature(failure_class, result)
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        failures = registry.setdefault("failures", [])
        if not isinstance(failures, list):
            failures = []
            registry["failures"] = failures
        existing: dict[str, Any] | None = None
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            if str(failure.get("failure_class") or "") != failure_class:
                continue
            if str(failure.get("action_scope_key") or "") != scope_key:
                continue
            if str(failure.get("status") or "") in FAILURE_TERMINAL_STATUSES:
                continue
            existing = failure
            break
        if existing is None:
            record = {
                "failure_id": _new_failure_id(),
                "task_id": "",
                "action_scope_key": scope_key,
                "run_id": run_id,
                "status": "unrecovered",
                "failure_class": failure_class,
                "failure_signature": signature,
                "summary": str(result.get("message") or "Scheduler action run failed."),
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "attempts": 1,
                # First failure has not consumed a recovery attempt yet, so the
                # immediate re-run below is the first retry within budget.
                "recovery_attempts": 0,
                "max_recovery_attempts": DEFAULT_MAX_RECOVERY_ATTEMPTS,
                "budget_remaining": True,
                "recoverable": True,
                "run_ids": [run_id] if run_id else [],
                "agent_status_path": result.get("agent_status_path"),
                "adapter_result_path": result.get("adapter_result_path"),
                "scheduler_run_dir": result.get("scheduler_run_dir"),
                "role_output_dir": result.get("role_output_dir"),
            }
            _refresh_failure_budget(record)
            failures.append(record)
            changed = dict(record)
            return
        # A repeat failure of the same action scope: a retry just ran and failed
        # again, so consume one recovery attempt. _refresh_failure_budget will flip
        # the entry to "exhausted" once the budget is spent.
        existing["recovery_attempts"] = _int_value(existing.get("recovery_attempts"), default=0) + 1
        existing["attempts"] = _int_value(existing.get("attempts"), default=0) + 1
        existing["last_seen_at"] = seen_at
        existing["run_id"] = run_id or existing.get("run_id")
        existing["summary"] = str(result.get("message") or existing.get("summary") or "Scheduler action run failed.")
        existing["failure_signature"] = signature or existing.get("failure_signature")
        existing["run_ids"] = _append_unique_string(existing.get("run_ids"), run_id)
        for path_field in ("agent_status_path", "adapter_result_path", "scheduler_run_dir", "role_output_dir"):
            if result.get(path_field):
                existing[path_field] = result[path_field]
        _refresh_failure_budget(existing)
        changed = dict(existing)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return dict(changed or {})


def _resolve_action_failure(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    failure_class: str,
    scope_key: str,
    result: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Mark any open action-failure entry for this scope as recovered after a
    successful re-run (mirrors how recovery_worker success resolves its failure)."""
    ended_at = str(result.get("ended_at") or utc_timestamp())
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        failures = registry.get("failures", [])
        if not isinstance(failures, list):
            return
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            if str(failure.get("failure_class") or "") != failure_class:
                continue
            if str(failure.get("action_scope_key") or "") != scope_key:
                continue
            if str(failure.get("status") or "") in FAILURE_TERMINAL_STATUSES:
                continue
            failure["status"] = "recovered"
            failure["last_seen_at"] = ended_at
            failure["last_recovery_ended_at"] = ended_at
            failure["last_recovery_status"] = result.get("status")
            failure["last_recovery_run_id"] = result.get("run_id")
            failure.pop("active_recovery_run_id", None)
            _refresh_failure_budget(failure)
            changed = dict(failure)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed


def _open_action_failure(
    snapshot: Mapping[str, Any], *, failure_class: str, scope_key: str
) -> Mapping[str, Any] | None:
    """Return the open (non-terminal) action-failure entry for a scope, if any."""
    registry = snapshot.get("failure_registry")
    failures = registry.get("failures", []) if isinstance(registry, Mapping) else []
    for failure in failures:
        if not isinstance(failure, Mapping):
            continue
        if str(failure.get("failure_class") or "") != failure_class:
            continue
        if str(failure.get("action_scope_key") or "") != scope_key:
            continue
        if str(failure.get("status") or "unrecovered") in FAILURE_TERMINAL_STATUSES:
            continue
        return failure
    return None


def _mark_failure_recovering(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    project: Path,
    failure_id: str,
    prepared: PreparedRun,
) -> dict[str, Any]:
    now = utc_timestamp()
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        failure = _failure_by_id(registry, failure_id)
        if failure is None:
            return
        if failure.get("active_recovery_run_id") != prepared.run_id:
            failure["recovery_attempts"] = _int_value(
                failure.get("recovery_attempts"), default=0
            ) + 1
        failure["status"] = "recovering"
        failure["budget_remaining"] = False
        failure["last_recovery_started_at"] = now
        failure["active_recovery_run_id"] = prepared.run_id
        failure["last_recovery_run_id"] = prepared.run_id
        failure["last_recovery_prompt_path"] = _path_for_record(project, prepared.prompt_path)
        failure["last_recovery_role_output_dir"] = _path_for_record(project, prepared.role_output_dir)
        failure["recovery_run_ids"] = _append_unique_string(failure.get("recovery_run_ids"), prepared.run_id)
        changed = dict(failure)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed or {}


def _finish_recovery_failure_update(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    project: Path,
    failure_id: str,
    result: Mapping[str, Any],
    agent_status_record: Mapping[str, Any] | None,
) -> dict[str, Any]:
    ended_at = str(result.get("ended_at") or utc_timestamp())
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        failure = _failure_by_id(registry, failure_id)
        if failure is None:
            return
        recovery_attempts = _int_value(failure.get("recovery_attempts"), default=0)
        max_recovery = _int_value(
            failure.get("max_recovery_attempts"), default=DEFAULT_MAX_RECOVERY_ATTEMPTS
        )
        recovery_signature = _recovery_failure_signature(result, agent_status_record)
        repeated_without_new_info = (
            not bool(result.get("ok"))
            and result.get("failure_scope") != "runner"
            and recovery_signature
            and recovery_signature == str(failure.get("failure_signature") or "")
            and not _agent_status_has_new_information(agent_status_record)
        )
        if bool(result.get("ok")):
            next_status = "recovered"
        elif repeated_without_new_info:
            next_status = "exhausted"
            failure["exhausted_reason"] = "recovery_repeated_identical_failure_without_new_information"
            failure.pop("needs_human_reason", None)
        elif recovery_attempts >= max_recovery:
            next_status = "exhausted"
            failure["exhausted_reason"] = "max_recovery_attempts_exhausted"
        else:
            next_status = "unrecovered"

        failure["status"] = next_status
        failure["last_seen_at"] = ended_at
        failure["last_recovery_ended_at"] = ended_at
        failure["last_recovery_status"] = result.get("status")
        failure["last_recovery_classification"] = result.get("classification")
        failure["last_recovery_message"] = result.get("message")
        failure["last_recovery_run_id"] = result.get("run_id")
        failure["last_recovery_adapter_result_path"] = result.get("adapter_result_path")
        if recovery_signature:
            failure["last_recovery_failure_signature"] = recovery_signature
        failure.pop("active_recovery_run_id", None)
        _refresh_failure_budget(failure)
        changed = dict(failure)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed or {"failure_id": failure_id, "status": "missing"}


def _defer_recovery_failure_for_runner_unavailability(
    *,
    paths: WorkflowPaths,
    workflow_id: str,
    failure_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    ended_at = str(result.get("ended_at") or utc_timestamp())
    run_id = str(result.get("run_id") or "")
    changed: dict[str, Any] | None = None

    def update(registry: dict[str, Any]) -> None:
        nonlocal changed
        failure = _failure_by_id(registry, failure_id)
        if failure is None:
            return
        if str(failure.get("active_recovery_run_id") or "") == run_id:
            attempts = _int_value(failure.get("recovery_attempts"), default=0)
            failure["recovery_attempts"] = max(0, attempts - 1)
        failure["status"] = "unrecovered"
        failure["last_seen_at"] = ended_at
        failure["last_recovery_ended_at"] = ended_at
        failure["last_recovery_status"] = result.get("status")
        failure["last_recovery_classification"] = result.get("classification")
        failure["last_recovery_message"] = result.get("message")
        failure["last_runner_unavailable_at"] = ended_at
        failure["last_runner_availability"] = _json_safe_object(result.get("runner_availability") or {})
        failure.pop("active_recovery_run_id", None)
        _refresh_failure_budget(failure)
        changed = dict(failure)

    _update_failure_registry_locked(paths, workflow_id=workflow_id, update=update)
    return changed or {"failure_id": failure_id, "status": "missing"}


def _upsert_failure(registry: dict[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    failures = registry.setdefault("failures", [])
    if not isinstance(failures, list):
        failures = []
        registry["failures"] = failures
    terminal_same_source = _matching_terminal_same_source(failures, candidate)
    if terminal_same_source is not None:
        return None
    terminal_same_identity = _matching_terminal_same_identity(failures, candidate)
    if terminal_same_identity is not None:
        _update_recurrent_terminal_failure(terminal_same_identity, candidate)
        return terminal_same_identity
    existing = _matching_failure(failures, candidate)
    if existing is None:
        record = dict(candidate)
        _refresh_failure_budget(record)
        failures.append(record)
        return record

    existing["last_seen_at"] = candidate.get("last_seen_at") or utc_timestamp()
    existing["attempts"] = _int_value(existing.get("attempts"), default=0) + 1
    existing["run_id"] = candidate.get("run_id") or existing.get("run_id")
    existing["summary"] = candidate.get("summary") or existing.get("summary")
    for path_field in ("source_validation_path", "agent_status_path", "adapter_result_path", "scheduler_run_dir", "role_output_dir"):
        if candidate.get(path_field):
            existing[path_field] = candidate[path_field]
    existing["run_ids"] = _append_unique_string(existing.get("run_ids"), candidate.get("run_id"))
    if str(existing.get("status") or "") in {"recovering"}:
        pass
    elif str(existing.get("status") or "") not in FAILURE_TERMINAL_STATUSES:
        existing["status"] = "unrecovered"
    _refresh_failure_budget(existing)
    return existing


def _matching_failure(failures: Sequence[Any], candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("status") or "unrecovered") in FAILURE_TERMINAL_STATUSES:
            continue
        if _same_failure_identity(failure, candidate):
            return failure
    return None


def _matching_terminal_same_source(failures: Sequence[Any], candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("status") or "") not in FAILURE_TERMINAL_STATUSES:
            continue
        if not _same_failure_identity(failure, candidate):
            continue
        if candidate.get("source_validation_path") and failure.get("source_validation_path") == candidate.get("source_validation_path"):
            return failure
        if candidate.get("run_id") and failure.get("run_id") == candidate.get("run_id"):
            return failure
    return None


def _matching_terminal_same_identity(failures: Sequence[Any], candidate: Mapping[str, Any]) -> dict[str, Any] | None:
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        if str(failure.get("status") or "") not in FAILURE_TERMINAL_STATUSES:
            continue
        if _same_failure_identity(failure, candidate):
            return failure
    return None


def _update_recurrent_terminal_failure(failure: dict[str, Any], candidate: Mapping[str, Any]) -> None:
    prior_status = str(failure.get("status") or "")
    failure["last_seen_at"] = candidate.get("last_seen_at") or utc_timestamp()
    failure["attempts"] = _int_value(failure.get("attempts"), default=0) + 1
    failure["run_id"] = candidate.get("run_id") or failure.get("run_id")
    failure["summary"] = candidate.get("summary") or failure.get("summary")
    for path_field in ("source_validation_path", "agent_status_path", "adapter_result_path", "scheduler_run_dir", "role_output_dir"):
        if candidate.get(path_field):
            failure[path_field] = candidate[path_field]
    failure["run_ids"] = _append_unique_string(failure.get("run_ids"), candidate.get("run_id"))
    if prior_status == "recovered":
        failure["status"] = "unrecovered"
        failure["reopened_reason"] = "validation_recurred_after_recovery"
        failure.pop("needs_human_reason", None)
    _refresh_failure_budget(failure)


def _same_failure_identity(failure: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return (
        str(failure.get("task_id") or "") == str(candidate.get("task_id") or "")
        and str(failure.get("failure_class") or "") == str(candidate.get("failure_class") or "")
        and str(failure.get("failure_signature") or "") == str(candidate.get("failure_signature") or "")
    )


def _failure_by_id(registry: Mapping[str, Any], failure_id: str) -> dict[str, Any] | None:
    failures = registry.get("failures", [])
    if not isinstance(failures, list):
        return None
    for failure in failures:
        if isinstance(failure, dict) and str(failure.get("failure_id") or "") == failure_id:
            return failure
    return None


def _refresh_failure_budget(failure: dict[str, Any]) -> None:
    status = str(failure.get("status") or "unrecovered")
    attempts = _int_value(failure.get("recovery_attempts"), default=0)
    budget = _int_value(
        failure.get("max_recovery_attempts"), default=DEFAULT_MAX_RECOVERY_ATTEMPTS
    )
    budget_remaining = status == "unrecovered" and attempts < budget
    failure["budget_remaining"] = budget_remaining
    if status == "unrecovered" and not budget_remaining:
        failure["status"] = "exhausted"
        failure["exhausted_reason"] = "max_recovery_attempts_exhausted"


def _validation_task_id(paths: WorkflowPaths, validation_path: Path, validation: Mapping[str, Any]) -> str:
    for field in ("primary_task_id", "task_id"):
        value = validation.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    try:
        relative = validation_path.relative_to(paths.results_dir)
    except ValueError:
        return ""
    return relative.parts[0] if relative.parts else ""


def _validation_failure_signature(validation: Mapping[str, Any]) -> str:
    explicit = validation.get("failure_signature") or validation.get("signature")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    checks = validation.get("checks")
    failed_checks: list[dict[str, str]] = []
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            if str(check.get("status") or "").lower() in {"fail", "failed", "rejected"}:
                failed_checks.append(
                    {
                        "name": str(check.get("name") or ""),
                        "message": str(check.get("message") or ""),
                    }
                )
    payload = {
        "status": validation.get("status"),
        "summary": validation.get("summary") or validation.get("message") or validation.get("reason"),
        "failed_checks": failed_checks,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "validation_failed:" + sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _worker_failure_signature(result: Mapping[str, Any]) -> str:
    parts = [
        str(result.get("classification") or "worker_failed"),
        str(result.get("status") or "failed"),
        f"exit_{result.get('adapter_exit_code')}",
        f"timed_out_{str(bool(result.get('adapter_timed_out'))).lower()}",
    ]
    return ":".join(parts)


def _recovery_failure_signature(
    result: Mapping[str, Any],
    agent_status_record: Mapping[str, Any] | None,
) -> str:
    if isinstance(agent_status_record, Mapping):
        explicit = agent_status_record.get("failure_signature") or agent_status_record.get("signature")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        scheduler_classification = agent_status_record.get("scheduler_classification")
        if isinstance(scheduler_classification, Mapping):
            classification = scheduler_classification.get("classification")
            if isinstance(classification, str) and classification.strip():
                return classification.strip()
    if not bool(result.get("ok")):
        return _worker_failure_signature(result)
    return ""


def _agent_status_has_new_information(agent_status_record: Mapping[str, Any] | None) -> bool:
    if not isinstance(agent_status_record, Mapping):
        return False
    for field in ("new_information", "has_new_information"):
        if agent_status_record.get(field) is True:
            return True
    repair_attempts = agent_status_record.get("repair_attempts")
    if isinstance(repair_attempts, Sequence) and not isinstance(repair_attempts, (str, bytes)):
        for attempt in repair_attempts:
            if not isinstance(attempt, Mapping):
                continue
            if attempt.get("new_information") is True:
                return True
            summary = attempt.get("new_information_summary")
            if isinstance(summary, str) and summary.strip():
                return True
    return False


def _append_unique_string(existing: Any, value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes)):
        values.extend(str(item) for item in existing if item is not None and str(item))
    if value is not None and str(value):
        values.append(str(value))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _task_max_attempts(snapshot: Mapping[str, Any], task_id: str) -> int:
    for task in snapshot.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        if str(task.get("task_id") or "") != task_id:
            continue
        return max(
            DEFAULT_MAX_RECOVERY_ATTEMPTS,
            _int_value(task.get("max_attempts"), default=DEFAULT_MAX_RECOVERY_ATTEMPTS),
        )
    return DEFAULT_MAX_RECOVERY_ATTEMPTS


def _new_failure_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"fail_{stamp}_{uuid.uuid4().hex[:8]}"


def _resolve_cwd(project: Path, cwd: str) -> Path:
    expanded = cwd.replace("{{project_root}}", project.as_posix())
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _run_final_verification(paths: WorkflowPaths, snapshot: Mapping[str, Any], *, owner: str) -> dict[str, Any]:
    from runtime.final_verifier import run_final_verifier

    return run_final_verifier(paths.project_root, owner=owner, write=True)


def run_objective_verifier_once(
    project_root: Path | str,
    *,
    scope: str = "auto",
    phase_id: str | None = None,
    owner: str = "cli",
    force: bool = False,
) -> dict[str, Any]:
    snapshot = load_scheduler_snapshot(project_root)
    if not snapshot.get("ok"):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "snapshot_unavailable",
            "errors": [str(snapshot.get("load_error") or "Unable to load scheduler snapshot.")],
            "snapshot": _json_safe_object(snapshot),
        }
    gate: dict[str, Any] | None = None
    normalized_scope = scope if scope in {"auto", "phase", "workflow"} else "auto"
    if normalized_scope == "phase" and phase_id:
        record = _objective_gate_record(snapshot, scope="phase", phase_id=phase_id)
        gate = _objective_gate_selected(snapshot, record) if record is not None else None
    elif normalized_scope == "phase":
        gate = _phase_objective_gate_candidate(snapshot)
    elif normalized_scope == "workflow":
        record = _objective_gate_record(snapshot, scope="workflow", phase_id=None)
        gate = _objective_gate_selected(snapshot, record) if record is not None else None
    else:
        gate = _phase_objective_gate_candidate(snapshot) or _final_objective_gate_candidate(snapshot)
    if gate is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "no_objective_verification_candidate",
            "message": "No objective verifier candidate is currently available.",
        }
    if not force and str(gate.get("objective_gate_status") or gate.get("status") or "") not in {"needs_verification"}:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "objective_verification_not_needed",
            "message": "The selected objective gate does not require verifier execution.",
            "objective_gate": gate,
        }
    runner = _runner_for_role(snapshot, "objective_verifier")
    if runner is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "waiting_config",
            "errors": ["No enabled objective verifier runner is configured."],
            "objective_gate": gate,
        }
    selected = {**gate, "runner_id": runner.runner_id, "runner_role": runner.role}
    action = _action(
        str(selected.get("action_name") or "run_phase_objective_verifier"),
        reason="Objective verifier manually selected.",
        selected=selected,
    )
    execution = execute_selected_action(snapshot, action, owner=owner)
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(execution and execution.get("ok")),
        "status": str(execution.get("status") if isinstance(execution, Mapping) else "not_executed"),
        "selected_action": action,
        "execution_result": execution,
    }


def _workflow_registry_completion_is_current(paths: WorkflowPaths, workflow_id: str, marker_path: str) -> bool:
    registry_path = paths.project_root / ".loopplane" / "workflow_registry.json"
    data = _read_json_object(registry_path, default={})
    workflows = data.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        return False
    for record in workflows:
        if not isinstance(record, Mapping):
            continue
        if record.get("workflow_id") != workflow_id:
            continue
        return str(record.get("status") or "") == "completed" and str(record.get("completion_marker") or "") == marker_path
    return False


def _append_jsonl_locked(paths: WorkflowPaths, path: Path, record: Mapping[str, Any]) -> None:
    owner = f"event-append:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    lock = AtomicOwnerLock(paths.runtime_dir / "lock" / "event_append_lock", owner, ttl_seconds=30)
    with lock.acquire():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")


def _record_task_approval_request(paths: WorkflowPaths, *, workflow_id: str, task: Mapping[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "unknown_task")
    existing = _pending_approval_request_for_task(
        _read_jsonl(paths.runtime_dir / APPROVAL_REQUESTS_FILENAME),
        _read_jsonl(paths.runtime_dir / APPROVAL_RESPONSES_FILENAME),
        task_id,
    )
    if existing is not None:
        return existing
    title = str(task.get("title") or task.get("summary") or task_id)
    request = {
        "schema_version": SCHEMA_VERSION,
        "approval_id": new_approval_id(),
        "created_at": utc_timestamp(),
        "workflow_id": workflow_id,
        "task_id": task_id,
        "run_id": None,
        "type": "task_execution",
        "message": f"Approve execution of {task_id}: {title}",
        "scope": f"{task_id} only",
        "expires_at": default_expires_at(),
        "status": "pending",
    }
    _append_jsonl_locked(paths, paths.runtime_dir / APPROVAL_REQUESTS_FILENAME, request)
    return request


def _requires_attention_item(selected: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
    task_id = selected.get("task_id")
    approval_id = selected.get("approval_id")
    request_id = approval_id or task_id or f"attention_{uuid.uuid4().hex[:8]}"
    item = {
        "type": str(selected.get("type") or selected.get("run_kind") or "requires_attention"),
        "request_id": str(request_id),
        "task_id": task_id,
        "approval_id": approval_id,
        "status": "requires_attention",
        "message": str(selected.get("message") or reason),
        "reason": reason,
        "created_at": utc_timestamp(),
    }
    if selected.get("approval_decision") is not None:
        item["approval_decision"] = selected.get("approval_decision")
    policy = selected.get("approval_policy")
    if isinstance(policy, Mapping):
        item["approval_policy"] = dict(policy)
    return {key: value for key, value in item.items() if value is not None}


def _update_runtime_state(
    paths: WorkflowPaths,
    *,
    status: str | None = None,
    scheduler_update: Mapping[str, Any] | None = None,
    requires_attention: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_object(state_path, default={})
    if status:
        state["status"] = status
    if requires_attention is not None:
        state["requires_attention"] = [dict(item) for item in requires_attention]
    scheduler = state.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
    scheduler.update(dict(scheduler_update or {}))
    scheduler["heartbeat_at"] = utc_timestamp()
    state["scheduler"] = scheduler
    _write_json(state_path, state)


def _load_runner_config(project: Path) -> tuple[Any | None, str | None]:
    try:
        return load_agent_runners(project), None
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError) as error:
        return None, str(error)


def _select_runner_for_prepare(
    project: Path,
    *,
    workflow_config: Mapping[str, Any],
    role: str,
    runner_id: str | None,
) -> RunnerConfig:
    try:
        config = load_agent_runners(project)
        if runner_id:
            runner = config.runner(runner_id)
            if not runner.enabled:
                raise SchedulerError(f"runner {runner_id!r} is disabled")
            if not _runner_role_matches_prepare_role(runner.role, role):
                raise SchedulerError(f"runner {runner_id!r} has role {runner.role!r}, not {role!r}")
            return runner

        paths = WorkflowPaths.from_config(project, workflow_config)
        snapshot = {
            "runner_config": config,
            "workflow_config": workflow_config,
            "runner_health": _read_runner_health(paths, workflow_id=_workflow_id(workflow_config)),
        }
        role_order = ("recovery_worker", "worker") if role == "recovery_worker" else (role,)
        for candidate_role in role_order:
            selected_runner = _runner_for_role(snapshot, candidate_role)
            if selected_runner is not None and _runner_role_matches_prepare_role(selected_runner.role, role):
                return selected_runner
            matches = list(config.runners_for_role(candidate_role, enabled_only=True))
            if candidate_role == "worker":
                preferred = workflow_config.get("default_worker_runner")
                for runner in matches:
                    if runner.runner_id == preferred:
                        return runner
            if matches:
                return matches[0]
    except AgentRunnerConfigError as error:
        raise SchedulerError(f"Unable to load runner for {role!r}: {error}") from error
    raise SchedulerError(f"No enabled runner is configured for role {role!r}")


def _runner_role_matches_prepare_role(runner_role: str, prepare_role: str) -> bool:
    if runner_role == prepare_role:
        return True
    return prepare_role == "recovery_worker" and runner_role == "worker"


def _role_output_dir(paths: WorkflowPaths, *, role: str, task_id: str | None, run_id: str) -> Path:
    if role in {"worker", "recovery_worker"}:
        if not isinstance(task_id, str) or not task_id.strip():
            raise SchedulerError(f"task_id is required for {role} runs")
        return paths.results_dir / task_id / "runs" / run_id
    if role in {"planner", "auditor"}:
        return paths.planning_dir / "runs" / run_id
    if role == "inspector":
        return paths.requests_dir / "inspections" / run_id
    if role == "change_request_planner":
        return paths.requests_dir / "change_runs" / run_id
    if role == "summary":
        return paths.runtime_dir / "summaries" / run_id
    if role == "expansion_planner":
        return paths.runtime_dir / "expansions" / run_id
    if role == "objective_verifier":
        return paths.runtime_dir / "objectives" / run_id
    if role == "final_reviewer":
        return paths.runtime_dir / "final_review" / run_id
    raise SchedulerError(f"unsupported run role: {role}")


def _role_uses_task_evidence(role: str) -> bool:
    return role in {"worker", "recovery_worker"}


def _new_runtime_run_id(paths: WorkflowPaths) -> str:
    for _ in range(100):
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        candidate = f"run_{stamp}_{uuid.uuid4().hex[:8]}"
        if not (paths.runtime_dir / "runs" / candidate).exists() and not (
            paths.runtime_dir / "active_run_leases" / f"{candidate}.json"
        ).exists():
            return candidate
    raise SchedulerError("unable to allocate unique run_id")


def _node_id(*, role: str, task_id: str | None, run_id: str) -> str:
    parts = ["node", _safe_id_part(role)]
    if task_id:
        parts.append(_safe_id_part(task_id))
    parts.append(_safe_id_part(run_id))
    return "_".join(part for part in parts if part)


def _safe_id_part(value: str) -> str:
    safe = [character if character.isalnum() else "_" for character in value.strip()]
    compact = "_".join(part for part in "".join(safe).split("_") if part)
    return compact or "unknown"


def _timestamp_after(timestamp: str, seconds: int) -> str:
    try:
        base = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if base.tzinfo is None:
            base = base.replace(tzinfo=UTC)
        base = base.astimezone(UTC)
    except ValueError:
        base = datetime.now(UTC).replace(microsecond=0)
    ttl = max(1, int(seconds))
    return (base + timedelta(seconds=ttl)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _path_for_record(project_root: Path | None, path: Path) -> str:
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _configuration_problems(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for problem in state.get("configuration_problems", []) if isinstance(state, Mapping) else []:
        if isinstance(problem, Mapping):
            normalized = dict(problem)
            normalized["code"] = str(normalized.get("code") or "configuration_problem")
            normalized["message"] = str(
                normalized.get("message") or normalized.get("reason") or "Workflow configuration requires attention."
            )
            problems.append(normalized)
        else:
            problems.append({"code": "configuration_problem", "message": str(problem)})
    if str(state.get("status", "")).lower() == "waiting_config" and not problems:
        problems.append({"code": "runtime_waiting_config", "message": "Runtime state is waiting_config."})
    return problems


def _task_approval_recorded(snapshot: Mapping[str, Any], task_id: str) -> bool:
    responses = [response for response in snapshot.get("approval_responses", []) if isinstance(response, Mapping)]
    return task_approval_decision(responses, task_id) == "approved"


def _read_json_object(path: Path, *, default: Any) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, Mapping) else default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, Mapping):
            records.append(dict(record))
    return records


def _read_background_jobs(paths: WorkflowPaths, *, workflow_id: str, now: datetime) -> list[dict[str, Any]]:
    path = paths.runtime_dir / BACKGROUND_JOBS_FILENAME
    registry, error = _read_background_job_registry(path, workflow_id=workflow_id)
    if error:
        return [
            {
                "job_id": "background_registry",
                "status": "needs_recovery",
                "next_prompt_ready": False,
                "status_problem": error,
                "wake_next_agent_when": (
                    f"Repair {paths.value('runtime_dir').rstrip('/')}/{BACKGROUND_JOBS_FILENAME} "
                    "before scheduling more work."
                ),
            }
        ]
    jobs: list[dict[str, Any]] = []
    persistent_change = False
    for index, raw_job in enumerate(registry.get("jobs", [])):
        if not isinstance(raw_job, Mapping):
            continue
        job = dict(raw_job)
        job.setdefault("job_id", f"background_job_{index + 1}")
        status = str(job.get("status") or "").strip().lower()
        if status not in ALLOWED_BACKGROUND_JOB_STATUSES:
            job["status"] = "needs_recovery"
            job["status_problem"] = f"invalid_status:{status or '<missing>'}"
            job["next_prompt_ready"] = False
        else:
            job["status"] = status
        evaluated = _evaluate_background_job_record(job, paths=paths, now=now)
        if _background_job_persistent_fields_changed(job, evaluated):
            persistent_change = True
        jobs.append(evaluated)
    if persistent_change:
        _write_background_job_registry(
            paths,
            {
                "schema_version": SCHEMA_VERSION,
                "workflow_id": workflow_id,
                "updated_at": utc_timestamp(),
                "jobs": [dict(job) for job in jobs],
            },
        )
    return jobs


def _background_job_persistent_fields_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    for field in (
        "status",
        "next_prompt_ready",
        "status_problem",
        "ended_at",
        "updated_at",
        "resolved_from_source_agent_status",
        "exit_code",
    ):
        if before.get(field) != after.get(field):
            return True
    return False


def _background_jobs_from_recent_agent_statuses(
    paths: WorkflowPaths,
    *,
    existing_jobs: Sequence[Mapping[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    existing_run_ids = {
        str(job.get("run_id") or "")
        for job in existing_jobs
        if isinstance(job, Mapping) and str(job.get("run_id") or "")
    }
    existing_job_ids = {
        str(job.get("job_id") or "")
        for job in existing_jobs
        if isinstance(job, Mapping) and str(job.get("job_id") or "")
    }
    inferred: list[dict[str, Any]] = []
    try:
        status_files = sorted(
            (path for path in paths.results_dir.glob("**/agent_status.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )[:20]
    except OSError:
        return inferred
    for status_path in status_files:
        status_record = _read_json_object(status_path, default={})
        if not isinstance(status_record, Mapping):
            continue
        worker_status = str(status_record.get("status") or "").lower()
        if not _agent_status_blocks_next_prompt(worker_status, status_record):
            continue
        run_id = _non_empty_text(status_record.get("run_id")) or status_path.parent.name
        if run_id in existing_run_ids:
            continue
        task_id = _non_empty_text(status_record.get("task_id") or status_record.get("primary_task_id"))
        if task_id is None:
            try:
                task_id = status_path.relative_to(paths.results_dir).parts[0]
            except (ValueError, IndexError):
                task_id = "unknown_task"
        raw_records = _agent_status_background_job_records(status_record)
        if not raw_records:
            raw_records = [
                {
                    "job_id": _background_job_id(task_id, run_id, 0),
                    "task_id": task_id,
                    "run_id": run_id,
                    "status": "running",
                    "wake_next_agent_when": _agent_wake_next_agent_when(status_record),
                    "logs": _background_list_values(status_record, "logs", "background_logs"),
                }
            ]
        for index, raw_record in enumerate(raw_records):
            job = dict(raw_record)
            job_id = _non_empty_text(job.get("job_id")) or _background_job_id(task_id, run_id, index)
            if job_id in existing_job_ids:
                continue
            job["job_id"] = job_id
            job.setdefault("task_id", task_id)
            job.setdefault("run_id", run_id)
            job.setdefault("status", "running")
            job["next_prompt_ready"] = _background_record_next_prompt_ready(
                job.get("next_prompt_ready"),
                status=str(job.get("status") or "running").strip().lower(),
                agent_status_record=status_record,
            )
            job.setdefault("wake_next_agent_when", _agent_wake_next_agent_when(status_record))
            job.setdefault("source_agent_status_path", _path_for_record(paths.project_root, status_path))
            job.setdefault("inferred_from_agent_status", True)
            if not job.get("heartbeat_at") and not job.get("started_at"):
                job["heartbeat_at"] = _non_empty_text(status_record.get("heartbeat_at") or status_record.get("ended_at") or status_record.get("started_at"))
            evaluated = _evaluate_background_job_record(job, paths=paths, now=now)
            inferred.append(evaluated)
            existing_run_ids.add(str(evaluated.get("run_id") or ""))
            existing_job_ids.add(str(evaluated.get("job_id") or ""))
    return inferred


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(dict(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _write_active_run_lease(path: Path, data: Mapping[str, Any]) -> None:
    _write_json(path, data)
    stamp_path = path.parent.parent / ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME
    try:
        stat_result = path.stat()
        _write_json(
            stamp_path,
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": utc_timestamp(),
                "lease_file": path.name,
                "lease_size_bytes": stat_result.st_size,
                "lease_mtime_ns": stat_result.st_mtime_ns,
                "update_id": uuid.uuid4().hex,
            },
        )
    except OSError:
        pass


def _append_event_unlocked(
    paths: WorkflowPaths,
    *,
    workflow_id: str,
    event_type: str,
    data: Mapping[str, Any],
    run_id: str | None,
    snapshot_interval: int | None,
) -> dict[str, Any]:
    event_path = paths.runtime_dir / "events" / EVENTS_SEGMENT
    event_path.parent.mkdir(parents=True, exist_ok=True)
    tail = _event_log_tail(event_path.parent)
    sequence = int(tail.get("sequence", 0)) + 1
    timestamp = utc_timestamp()
    payload = _json_safe_object(dict(data))
    event_id = _event_id_for_sequence(sequence)
    payload_record = _event_payload_for_log(
        paths,
        event_id=event_id,
        event_type=event_type,
        workflow_id=workflow_id,
        run_id=run_id,
        sequence=sequence,
        payload=payload,
    )
    compact_payload = payload_record["payload"]
    record = {
        "schema_version": SCHEMA_VERSION,
        "seq": sequence,
        "sequence": sequence,
        "event_id": event_id,
        "prev_event_id": tail.get("event_id"),
        "prev_event_hash": tail.get("event_hash"),
        "ts": timestamp,
        "timestamp": timestamp,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "event_type": event_type,
        "subject": _event_subject(payload if isinstance(payload, Mapping) else {}, run_id=run_id),
        "ui": {"title": event_type, "summary": "", "severity": "info", "visible": True},
        "refs": {},
        "payload": compact_payload,
        "data": compact_payload,
    }
    if payload_record.get("compacted"):
        record.update(
            {
                "payload_compacted": True,
                "payload_ref": payload_record["payload_ref"],
                "payload_sha256": payload_record["payload_sha256"],
                "payload_size_bytes": payload_record["payload_size_bytes"],
                "payload_summary": payload_record["payload_summary"],
            }
        )
    record["event_hash"] = _event_hash(record)
    _append_jsonl_fsynced(event_path, record)
    try:
        _update_event_segment_manifest_after_append(paths.runtime_dir / "events", event_path=event_path, record=record)
    except OSError:
        pass
    if snapshot_interval is not None and snapshot_interval > 0 and sequence % snapshot_interval == 0:
        _write_event_snapshot_unlocked(paths, workflow_id=workflow_id, through_sequence=sequence)
    return record


def _event_id_for_sequence(sequence: int) -> str:
    return f"evt_{sequence:012d}"


def _event_subject(data: Mapping[str, Any], *, run_id: str | None) -> dict[str, Any]:
    subject: dict[str, Any] = {}
    if run_id is not None:
        subject["run_id"] = run_id
    for key in ("task_id", "node_id", "failure_id", "request_id", "approval_id"):
        value = data.get(key)
        if value is not None:
            subject[key] = value
    return subject


def _event_payload_for_log(
    paths: WorkflowPaths,
    *,
    event_id: str,
    event_type: str,
    workflow_id: str,
    run_id: str | None,
    sequence: int,
    payload: Any,
) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    size_bytes = len(encoded)
    if size_bytes <= EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES:
        return {"payload": payload, "compacted": False}
    digest = "sha256:" + sha256(encoded).hexdigest()
    ref = f"{paths.value('runtime_dir').rstrip('/')}/{EVENT_PAYLOAD_SIDECAR_DIR}/{event_id}.json"
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "sequence": sequence,
        "payload_sha256": digest,
        "payload_size_bytes": size_bytes,
        "payload": payload,
    }
    _write_json_atomic_fsynced(paths.runtime_dir / EVENT_PAYLOAD_SIDECAR_DIR / f"{event_id}.json", sidecar)
    return {
        "payload": _compact_event_payload(event_type, payload),
        "compacted": True,
        "payload_ref": ref,
        "payload_sha256": digest,
        "payload_size_bytes": size_bytes,
        "payload_summary": _event_payload_summary(payload),
    }


def _compact_event_payload(event_type: str, payload: Any) -> dict[str, Any]:
    source = payload if isinstance(payload, Mapping) else {}
    compact: dict[str, Any] = {"event_payload_compacted": True}
    for key in (
        "task_id",
        "run_id",
        "role",
        "runner_id",
        "status",
        "message",
        "severity",
        "phase_id",
        "objective_id",
        "objective_phase_id",
        "failure_id",
        "proposal_id",
        "expansion_type",
        "proposal_path",
        "resolution_strategy",
        "risk",
        "failure_resolution_status",
        "approval",
        "started_at",
        "ended_at",
        "adapter_result_path",
        "agent_status_path",
        "context_manifest_path",
        "prompt_path",
        "stdout_path",
        "stderr_path",
        "final_output_path",
    ):
        if key in source:
            compact[key] = _compact_payload_value(source.get(key), depth=1)
    for key in ("target_task_ids", "target_failure_ids", "added_task_ids", "errors", "warnings"):
        if key in source:
            compact[key] = _compact_payload_value(source.get(key), depth=1)
    for key in ("selected_candidate", "proposal_apply", "agent_status", "selected_objective_gate"):
        value = source.get(key)
        if isinstance(value, Mapping):
            compact[f"{key}_summary"] = _event_payload_summary(value)
    compact["payload_summary"] = _event_payload_summary(source)
    compact["compacted_event_type"] = event_type
    return compact


def _event_payload_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        summary: dict[str, Any] = {"type": "object", "keys": len(value)}
        for key in (
            "status",
            "message",
            "task_id",
            "run_id",
            "proposal_id",
            "expansion_type",
            "resolution_strategy",
            "risk",
            "failure_resolution_status",
            "selected_failure_id",
        ):
            if key in value:
                summary[key] = _compact_payload_value(value.get(key), depth=0)
        for key in ("errors", "warnings", "added_task_ids", "target_task_ids", "target_failure_ids"):
            child = value.get(key)
            if isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
                summary[f"{key}_count"] = len(child)
        return summary
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"type": "array", "items": len(value)}
    return {"type": type(value).__name__}


def _compact_payload_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, Mapping):
        if depth <= 0:
            return _event_payload_summary(value)
        return {
            str(key): _compact_payload_value(item, depth=depth - 1)
            for key, item in list(value.items())[:12]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = [_compact_payload_value(item, depth=depth - 1) for item in list(value)[:20]]
        if len(value) > len(items):
            items.append({"truncated": True, "total_items": len(value)})
        return items
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + " [truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _event_hash(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("event_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _append_jsonl_fsynced(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)


def load_event_segment_manifest(events_dir: Path) -> dict[str, Any] | None:
    manifest = _read_event_segment_manifest(events_dir)
    if not manifest:
        return None
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        return None
    entries = {
        str(entry.get("path") or ""): entry
        for entry in segments
        if isinstance(entry, Mapping) and str(entry.get("path") or "")
    }
    event_paths = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    if set(entries) != {path.name for path in event_paths}:
        return None
    for path in event_paths:
        entry = entries.get(path.name)
        if not isinstance(entry, Mapping):
            return None
        try:
            stat_result = path.stat()
        except OSError:
            return None
        if int(entry.get("size_bytes") or -1) != int(stat_result.st_size):
            return None
        if int(entry.get("mtime_ns") or -1) != int(stat_result.st_mtime_ns):
            return None
    return dict(manifest)


def repair_event_segment_manifest(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    try:
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "manifest_path": None,
            "modified_files": [],
            "errors": [str(error)],
            "warnings": [],
        }
    events_dir = paths.runtime_dir / "events"
    manifest_path = events_dir / EVENTS_MANIFEST_FILENAME
    try:
        event_paths = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": paths.workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "manifest_path": _path_for_record(project, manifest_path),
            "modified_files": [],
            "errors": [str(error)],
            "warnings": [],
        }
    if not event_paths:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "status": "no_events",
            "project_root": project.as_posix(),
            "workflow_id": paths.workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "manifest_path": _path_for_record(project, manifest_path),
            "event_count": 0,
            "segment_count": 0,
            "modified_files": [],
            "errors": [],
            "warnings": [],
        }
    segments = [
        entry
        for entry in (_event_segment_entry_from_scan(path) for path in event_paths)
        if entry is not None
    ]
    event_count = sum(int(entry.get("record_count") or 0) for entry in segments)
    latest_segment = max(segments, key=lambda entry: int(entry.get("last_sequence") or 0), default=None)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": started_at,
        "events_dir": events_dir.name,
        "event_count": event_count,
        "latest_event": _manifest_event_head(latest_segment, prefix="last") if latest_segment else None,
        "segments": segments,
    }
    existing = _read_event_segment_manifest(events_dir)
    changed = _event_segment_manifest_semantic(existing) != _event_segment_manifest_semantic(manifest)
    modified_files: list[str] = []
    if changed and not dry_run:
        _write_json_atomic_fsynced(manifest_path, manifest)
        modified_files.append(_path_for_record(project, manifest_path))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": "planned" if changed and dry_run else "repaired" if changed else "current",
        "project_root": project.as_posix(),
        "workflow_id": paths.workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "manifest_path": _path_for_record(project, manifest_path),
        "event_count": event_count,
        "segment_count": len(segments),
        "latest_event": manifest["latest_event"],
        "modified_files": modified_files,
        "errors": [],
        "warnings": [],
    }


def compact_historical_event_payloads(
    project_root: Path | str,
    *,
    workflow_id: str | None = None,
    dry_run: bool = False,
    threshold_bytes: int | None = None,
    compact_all_events: bool = False,
) -> dict[str, Any]:
    project = Path(project_root).expanduser().resolve()
    started_at = utc_timestamp()
    threshold = EVENT_PAYLOAD_SIDECAR_THRESHOLD_BYTES if threshold_bytes is None else max(0, int(threshold_bytes))
    try:
        workflow_config = load_workflow_config(project, workflow_id=workflow_id)
        paths = WorkflowPaths.from_config(project, workflow_config)
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "modified_files": [],
            "errors": [str(error)],
            "warnings": [],
        }

    snapshot = load_latest_event_snapshot(paths)
    after_sequence = 0 if compact_all_events else _snapshot_sequence(snapshot)
    events_dir = paths.runtime_dir / "events"
    try:
        segment_paths = sorted(path for path in events_dir.glob("*.jsonl") if path.is_file())
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "project_root": project.as_posix(),
            "workflow_id": paths.workflow_id,
            "started_at": started_at,
            "ended_at": utc_timestamp(),
            "modified_files": [],
            "errors": [str(error)],
            "warnings": [],
        }
    if not segment_paths:
        return _event_payload_compaction_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="no_events",
            dry_run=dry_run,
            threshold=threshold,
            after_sequence=after_sequence,
            compact_all_events=compact_all_events,
        )

    rewritten_segments: dict[Path, list[dict[str, Any]]] = {}
    previous_hash: str | None = None
    previous_event_id: str | None = None
    compressed_records = 0
    candidate_records = 0
    bytes_before = 0
    bytes_after = 0
    for path in segment_paths:
        records = _read_jsonl(path)
        rewritten: list[dict[str, Any]] = []
        segment_changed = False
        for record in records:
            next_record = dict(record)
            sequence = _event_sequence_value(next_record)
            should_consider = sequence is not None and sequence > after_sequence
            if should_consider:
                payload = _historical_event_payload(next_record)
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                bytes_before += len(encoded)
                if next_record.get("payload_compacted") is not True and len(encoded) > threshold:
                    candidate_records += 1
                    payload_record = _event_payload_for_compaction(
                        paths,
                        event_id=str(next_record.get("event_id") or _event_id_for_sequence(sequence)),
                        event_type=str(next_record.get("event_type") or "event"),
                        workflow_id=str(next_record.get("workflow_id") or paths.workflow_id),
                        run_id=str(next_record.get("run_id")) if next_record.get("run_id") is not None else None,
                        sequence=sequence,
                        payload=payload,
                        write_sidecar=not dry_run,
                    )
                    next_record["payload"] = payload_record["payload"]
                    next_record["data"] = payload_record["payload"]
                    next_record["payload_compacted"] = True
                    next_record["payload_ref"] = payload_record["payload_ref"]
                    next_record["payload_sha256"] = payload_record["payload_sha256"]
                    next_record["payload_size_bytes"] = payload_record["payload_size_bytes"]
                    next_record["payload_summary"] = payload_record["payload_summary"]
                    compressed_records += 1
                    segment_changed = True
                    bytes_after += len(json.dumps(payload_record["payload"], sort_keys=True, separators=(",", ":")).encode("utf-8"))
                else:
                    bytes_after += len(encoded)
                if next_record.get("prev_event_hash") != previous_hash or next_record.get("prev_event_id") != previous_event_id:
                    next_record["prev_event_hash"] = previous_hash
                    next_record["prev_event_id"] = previous_event_id
                    segment_changed = True
                new_hash = _event_hash(next_record)
                if next_record.get("event_hash") != new_hash:
                    next_record["event_hash"] = new_hash
                    segment_changed = True
            rewritten.append(next_record)
            previous_hash = str(next_record.get("event_hash")) if isinstance(next_record.get("event_hash"), str) else None
            previous_event_id = str(next_record.get("event_id")) if isinstance(next_record.get("event_id"), str) else None
        if segment_changed:
            rewritten_segments[path] = rewritten

    if not rewritten_segments:
        return _event_payload_compaction_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="current",
            dry_run=dry_run,
            threshold=threshold,
            after_sequence=after_sequence,
            compact_all_events=compact_all_events,
            event_count=sum(len(_read_jsonl(path)) for path in segment_paths),
            segment_count=len(segment_paths),
            candidate_records=candidate_records,
            compressed_records=0,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
        )

    validation_errors = _event_chain_validation_errors(
        [record for path in segment_paths for record in rewritten_segments.get(path, _read_jsonl(path))]
    )
    if validation_errors:
        return _event_payload_compaction_result(
            project=project,
            paths=paths,
            started_at=started_at,
            status="validation_failed",
            dry_run=dry_run,
            threshold=threshold,
            after_sequence=after_sequence,
            compact_all_events=compact_all_events,
            event_count=sum(len(_read_jsonl(path)) for path in segment_paths),
            segment_count=len(segment_paths),
            candidate_records=candidate_records,
            compressed_records=compressed_records,
            bytes_before=bytes_before,
            bytes_after=bytes_after,
            errors=validation_errors[:10],
        )

    modified_files: list[str] = []
    backup_dir: Path | None = None
    if not dry_run:
        backup_dir = events_dir / "backups" / f"event_payload_compaction_{started_at.replace('-', '').replace(':', '').rstrip('Z')}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path, records in rewritten_segments.items():
            shutil.copy2(path, backup_dir / path.name)
            _write_jsonl_atomic_fsynced(path, records)
            modified = _path_for_record(project, path)
            if modified is not None:
                modified_files.append(modified)
        repair = repair_event_segment_manifest(project, workflow_id=paths.workflow_id, dry_run=False)
        for modified in repair.get("modified_files") or []:
            if isinstance(modified, str) and modified not in modified_files:
                modified_files.append(modified)

    return _event_payload_compaction_result(
        project=project,
        paths=paths,
        started_at=started_at,
        status="planned" if dry_run else "compacted",
        dry_run=dry_run,
        threshold=threshold,
        after_sequence=after_sequence,
        compact_all_events=compact_all_events,
        event_count=sum(len(_read_jsonl(path)) for path in segment_paths),
        segment_count=len(segment_paths),
        candidate_records=candidate_records,
        compressed_records=compressed_records,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        modified_files=modified_files,
        backup_dir=_path_for_record(project, backup_dir) if backup_dir is not None else None,
    )


def _event_payload_compaction_result(
    *,
    project: Path,
    paths: WorkflowPaths,
    started_at: str,
    status: str,
    dry_run: bool,
    threshold: int,
    after_sequence: int,
    compact_all_events: bool,
    event_count: int = 0,
    segment_count: int = 0,
    candidate_records: int = 0,
    compressed_records: int = 0,
    bytes_before: int = 0,
    bytes_after: int = 0,
    modified_files: Sequence[str] | None = None,
    backup_dir: str | None = None,
    errors: Sequence[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": paths.workflow_id,
        "started_at": started_at,
        "ended_at": utc_timestamp(),
        "dry_run": dry_run,
        "threshold_bytes": threshold,
        "after_sequence": after_sequence,
        "compact_all_events": compact_all_events,
        "event_count": event_count,
        "segment_count": segment_count,
        "candidate_records": candidate_records,
        "compressed_records": compressed_records,
        "payload_bytes_before": bytes_before,
        "payload_bytes_after": bytes_after,
        "payload_bytes_saved": max(0, bytes_before - bytes_after),
        "backup_dir": backup_dir,
        "modified_files": list(modified_files or []),
        "errors": list(errors or []),
        "warnings": [],
    }


def _historical_event_payload(record: Mapping[str, Any]) -> Any:
    payload = record.get("payload")
    if payload is not None:
        return payload
    return record.get("data") if record.get("data") is not None else {}


def _event_payload_for_compaction(
    paths: WorkflowPaths,
    *,
    event_id: str,
    event_type: str,
    workflow_id: str,
    run_id: str | None,
    sequence: int,
    payload: Any,
    write_sidecar: bool,
) -> dict[str, Any]:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = "sha256:" + sha256(encoded).hexdigest()
    ref = f"{paths.value('runtime_dir').rstrip('/')}/{EVENT_PAYLOAD_SIDECAR_DIR}/{event_id}.json"
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "sequence": sequence,
        "payload_sha256": digest,
        "payload_size_bytes": len(encoded),
        "payload": payload,
    }
    if write_sidecar:
        _write_json_atomic_fsynced(paths.runtime_dir / EVENT_PAYLOAD_SIDECAR_DIR / f"{event_id}.json", sidecar)
    return {
        "payload": _compact_event_payload(event_type, payload),
        "payload_ref": ref,
        "payload_sha256": digest,
        "payload_size_bytes": len(encoded),
        "payload_summary": _event_payload_summary(payload),
    }


def _event_chain_validation_errors(records: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    previous_hash: str | None = None
    previous_event_id: str | None = None
    previous_sequence: int | None = None
    for record in records:
        sequence = _event_sequence_value(record)
        event_id = str(record.get("event_id") or "")
        if previous_sequence is not None and sequence is not None and sequence != previous_sequence + 1:
            errors.append(f"{event_id or sequence}: expected sequence {previous_sequence + 1}, found {sequence}")
        if record.get("prev_event_hash") != previous_hash:
            errors.append(f"{event_id or sequence}: prev_event_hash mismatch")
        if record.get("prev_event_id") != previous_event_id:
            errors.append(f"{event_id or sequence}: prev_event_id mismatch")
        if record.get("event_hash") != _event_hash(record):
            errors.append(f"{event_id or sequence}: event_hash mismatch")
        if len(errors) >= 20:
            break
        previous_hash = str(record.get("event_hash")) if isinstance(record.get("event_hash"), str) else None
        previous_event_id = str(record.get("event_id")) if isinstance(record.get("event_id"), str) else None
        previous_sequence = sequence if sequence is not None else previous_sequence
    return errors


def _write_jsonl_atomic_fsynced(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = "".join(json.dumps(dict(record), sort_keys=True, separators=(",", ":")) + "\n" for record in records).encode("utf-8")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, encoded)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    temp_path.replace(path)
    _fsync_directory(path.parent)


def _event_segment_manifest_semantic(value: Mapping[str, Any] | None) -> Any:
    if not isinstance(value, Mapping):
        return None
    return {
        str(key): item
        for key, item in value.items()
        if str(key) != "generated_at"
    }


def _read_event_segment_manifest(events_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((events_dir / EVENTS_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _update_event_segment_manifest_after_append(
    events_dir: Path,
    *,
    event_path: Path,
    record: Mapping[str, Any],
) -> None:
    previous = _read_event_segment_manifest(events_dir) or {}
    previous_entries = {
        str(entry.get("path") or ""): entry
        for entry in previous.get("segments", [])
        if isinstance(entry, Mapping) and str(entry.get("path") or "")
    }
    segments = []
    for path in sorted(path for path in events_dir.glob("*.jsonl") if path.is_file()):
        previous_entry = previous_entries.get(path.name)
        if path == event_path:
            entry = _event_segment_entry_after_append(path, previous_entry=previous_entry, record=record)
        elif isinstance(previous_entry, Mapping) and _event_segment_entry_stat_matches(path, previous_entry):
            entry = dict(previous_entry)
        else:
            entry = _event_segment_entry_from_scan(path)
        if entry is not None:
            segments.append(entry)
    event_count = sum(int(entry.get("record_count") or 0) for entry in segments)
    latest_segment = max(
        segments,
        key=lambda entry: int(entry.get("last_sequence") or 0),
        default=None,
    )
    latest_event = _manifest_event_head(latest_segment, prefix="last") if latest_segment else None
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "events_dir": events_dir.name,
        "event_count": event_count,
        "latest_event": latest_event,
        "segments": segments,
    }
    _write_json_atomic_fsynced(events_dir / EVENTS_MANIFEST_FILENAME, manifest)


def _event_segment_entry_after_append(
    path: Path,
    *,
    previous_entry: Mapping[str, Any] | None,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(previous_entry, Mapping) and int(previous_entry.get("last_sequence") or 0) == (_event_sequence_value(record) or 0) - 1:
        try:
            stat_result = path.stat()
        except OSError:
            stat_result = None
        entry = dict(previous_entry)
        if stat_result is not None:
            entry["size_bytes"] = int(stat_result.st_size)
            entry["mtime_ns"] = int(stat_result.st_mtime_ns)
        entry["last_sequence"] = _event_sequence_value(record)
        entry["last_event_id"] = record.get("event_id")
        entry["last_event_hash"] = record.get("event_hash")
        entry["last_event_type"] = record.get("event_type")
        entry["last_timestamp"] = record.get("ts") or record.get("timestamp")
        entry["record_count"] = int(entry.get("record_count") or 0) + 1
        entry["contains_payload_refs"] = bool(entry.get("contains_payload_refs")) or record.get("payload_compacted") is True
        return entry
    scanned = _event_segment_entry_from_scan(path)
    if scanned is not None:
        return scanned
    try:
        stat_result = path.stat()
    except OSError:
        size_bytes = 0
        mtime_ns = 0
    else:
        size_bytes = int(stat_result.st_size)
        mtime_ns = int(stat_result.st_mtime_ns)
    return {
        "path": path.name,
        "size_bytes": size_bytes,
        "mtime_ns": mtime_ns,
        "first_sequence": _event_sequence_value(record),
        "last_sequence": _event_sequence_value(record),
        "first_event_id": record.get("event_id"),
        "last_event_id": record.get("event_id"),
        "first_event_hash": record.get("event_hash"),
        "last_event_hash": record.get("event_hash"),
        "first_event_type": record.get("event_type"),
        "last_event_type": record.get("event_type"),
        "first_timestamp": record.get("ts") or record.get("timestamp"),
        "last_timestamp": record.get("ts") or record.get("timestamp"),
        "record_count": 1,
        "contains_payload_refs": record.get("payload_compacted") is True,
    }


def _event_segment_entry_stat_matches(path: Path, entry: Mapping[str, Any]) -> bool:
    try:
        stat_result = path.stat()
    except OSError:
        return False
    return (
        int(entry.get("size_bytes") or -1) == int(stat_result.st_size)
        and int(entry.get("mtime_ns") or -1) == int(stat_result.st_mtime_ns)
    )


def _event_segment_entry_from_scan(path: Path) -> dict[str, Any] | None:
    records = _read_jsonl(path)
    if not records:
        return None
    try:
        stat_result = path.stat()
    except OSError:
        size_bytes = 0
        mtime_ns = 0
    else:
        size_bytes = int(stat_result.st_size)
        mtime_ns = int(stat_result.st_mtime_ns)
    first = records[0]
    last = records[-1]
    return {
        "path": path.name,
        "size_bytes": size_bytes,
        "mtime_ns": mtime_ns,
        "first_sequence": _event_sequence_value(first),
        "last_sequence": _event_sequence_value(last),
        "first_event_id": first.get("event_id"),
        "last_event_id": last.get("event_id"),
        "first_event_hash": first.get("event_hash"),
        "last_event_hash": last.get("event_hash"),
        "first_event_type": first.get("event_type"),
        "last_event_type": last.get("event_type"),
        "first_timestamp": first.get("ts") or first.get("timestamp"),
        "last_timestamp": last.get("ts") or last.get("timestamp"),
        "record_count": len(records),
        "contains_payload_refs": any(record.get("payload_compacted") is True for record in records),
    }


def _manifest_event_head(segment: Mapping[str, Any] | None, *, prefix: str) -> dict[str, Any] | None:
    if not isinstance(segment, Mapping):
        return None
    sequence = segment.get(f"{prefix}_sequence")
    event_id = segment.get(f"{prefix}_event_id")
    event_hash = segment.get(f"{prefix}_event_hash")
    if sequence is None and event_id is None and event_hash is None:
        return None
    return {
        "seq": sequence,
        "sequence": sequence,
        "event_id": event_id,
        "event_hash": event_hash,
        "event_type": segment.get(f"{prefix}_event_type"),
        "ts": segment.get(f"{prefix}_timestamp"),
    }


def _event_log_tail(events_dir: Path) -> dict[str, Any]:
    manifest = load_event_segment_manifest(events_dir)
    latest_event = manifest.get("latest_event") if isinstance(manifest, Mapping) else None
    if isinstance(latest_event, Mapping):
        sequence = _event_sequence_value(latest_event)
        if sequence is not None:
            return {
                "sequence": sequence,
                "event_id": latest_event.get("event_id"),
                "event_hash": latest_event.get("event_hash"),
            }
    tail: dict[str, Any] = {"sequence": 0, "event_id": None, "event_hash": None}
    for record in _iter_event_records(events_dir):
        sequence = _event_sequence_value(record)
        if sequence is None:
            continue
        if sequence >= int(tail["sequence"]):
            tail = {
                "sequence": sequence,
                "event_id": record.get("event_id"),
                "event_hash": record.get("event_hash"),
            }
    return tail


def _iter_event_records(events_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for segment in sorted(path for path in events_dir.glob("*.jsonl") if path.is_file()):
        records.extend(_read_jsonl(segment))
    return records


def _event_sequence_value(record: Mapping[str, Any]) -> int | None:
    value = record.get("seq", record.get("sequence"))
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_event_snapshot_unlocked(
    paths: WorkflowPaths,
    *,
    workflow_id: str | None,
    through_sequence: int | None,
) -> Path:
    events = _iter_event_records(paths.runtime_dir / "events")
    if through_sequence is None:
        through_sequence = max((_event_sequence_value(record) or 0 for record in events), default=0)
    projection = _empty_event_projection(workflow_id)
    event_log_head: dict[str, Any] | None = None
    for record in events:
        sequence = _event_sequence_value(record)
        if sequence is None or sequence > through_sequence:
            continue
        _apply_event_to_projection(projection, record)
        event_log_head = _compact_event_head(record)
    snapshot_id = f"snapshot_{through_sequence:06d}"
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "workflow_id": workflow_id or projection.get("workflow_id"),
        "created_at": utc_timestamp(),
        "events_through_sequence": through_sequence,
        "event_log_head": event_log_head,
        "state": projection,
    }
    snapshot_path = paths.runtime_dir / "snapshots" / f"{snapshot_id}.json"
    _write_json_atomic_fsynced(snapshot_path, snapshot)
    return snapshot_path


def _snapshot_sequence(snapshot: Mapping[str, Any]) -> int:
    value = snapshot.get("events_through_sequence", snapshot.get("sequence", 0))
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _empty_event_projection(workflow_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "event_count": 0,
        "event_type_counts": {},
        "latest_event": None,
        "latest_event_id": None,
        "latest_event_hash": None,
    }


def _projection_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    state = snapshot.get("state")
    if isinstance(state, Mapping) and isinstance(state.get("event_type_counts"), Mapping):
        projection = dict(state)
        projection["event_type_counts"] = dict(state.get("event_type_counts") or {})
        return projection
    return _empty_event_projection(str(snapshot.get("workflow_id")) if snapshot.get("workflow_id") else None)


def _apply_event_to_projection(projection: dict[str, Any], record: Mapping[str, Any]) -> None:
    sequence = _event_sequence_value(record)
    if sequence is None:
        return
    event_type = str(record.get("event_type") or "unknown")
    counts = projection.setdefault("event_type_counts", {})
    if not isinstance(counts, dict):
        counts = {}
        projection["event_type_counts"] = counts
    counts[event_type] = int(counts.get(event_type, 0)) + 1
    projection["workflow_id"] = projection.get("workflow_id") or record.get("workflow_id")
    projection["event_count"] = max(int(projection.get("event_count") or 0), sequence)
    projection["latest_event"] = _compact_event_head(record)
    projection["latest_event_id"] = record.get("event_id")
    projection["latest_event_hash"] = record.get("event_hash")


def _compact_event_head(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": _event_sequence_value(record),
        "event_id": record.get("event_id"),
        "event_hash": record.get("event_hash"),
        "event_type": record.get("event_type"),
        "ts": record.get("ts") or record.get("timestamp"),
        "workflow_id": record.get("workflow_id"),
        "run_id": record.get("run_id"),
    }


def _write_json_atomic_fsynced(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = (json.dumps(dict(data), indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, encoded)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(fd)
    os.replace(temp_path, path)
    _fsync_directory(path.parent)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    total = 0
    while total < len(data):
        written = os.write(fd, view[total:])
        if written == 0:
            raise OSError("short write while flushing event log data")
        total += written


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _owner_metadata(owner: str, ttl_seconds: int) -> dict[str, Any]:
    now = utc_timestamp()
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "lock_id": uuid.uuid4().hex,
        "owner": owner,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": now,
        "heartbeat_at": now,
        "ttl_seconds": ttl_seconds,
    }
    process_start_time = _process_start_time(os.getpid())
    if process_start_time is not None:
        metadata["process_start_time"] = process_start_time
    return metadata


def _lock_owner_pid(owner: Mapping[str, Any]) -> int | None:
    pid = _positive_int(owner.get("pid"))
    if pid is not None:
        return pid
    owner_text = str(owner.get("owner") or "")
    parts = owner_text.split(":")
    if len(parts) >= 2:
        return _positive_int(parts[1])
    return None


def _process_start_time(pid: int) -> str | None:
    try:
        stat_fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
        return f"proc:{stat_fields[19]}"
    except (OSError, IndexError):
        return None


def _hostnames_match(left: str, right: str) -> bool:
    left_normalized = left.strip().lower().rstrip(".")
    right_normalized = right.strip().lower().rstrip(".")
    if not left_normalized or not right_normalized:
        return False
    return left_normalized == right_normalized or left_normalized.split(".", 1)[0] == right_normalized.split(".", 1)[0]


def _unlink_owned_lock_path(path: Path, *, fd: int, lock_id: str | None) -> bool:
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
        return False
    if lock_id is not None:
        owner = _read_json_object(path, default={})
        if not isinstance(owner, Mapping) or str(owner.get("lock_id") or "") != lock_id:
            return False
    try:
        current_stat = path.stat()
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (current_stat.st_dev, current_stat.st_ino):
            return False
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _unlink_lock_path_if_unchanged(
    path: Path,
    *,
    observed_stat: os.stat_result,
    lock_id: str | None,
    heartbeat_at: str | None,
) -> bool:
    try:
        current_stat = path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    observed_version = (
        observed_stat.st_dev,
        observed_stat.st_ino,
        observed_stat.st_size,
        observed_stat.st_mtime_ns,
    )
    current_version = (
        current_stat.st_dev,
        current_stat.st_ino,
        current_stat.st_size,
        current_stat.st_mtime_ns,
    )
    if current_version != observed_version:
        return False
    current_owner = _read_json_object(path, default={})
    if not isinstance(current_owner, Mapping):
        return False
    if lock_id is not None and _non_empty_text(current_owner.get("lock_id")) != lock_id:
        return False
    if heartbeat_at is not None and _non_empty_text(current_owner.get("heartbeat_at")) != heartbeat_at:
        return False
    try:
        final_stat = path.stat()
        if (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
        ) != current_version:
            return False
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _scheduler_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _record_id(record: Mapping[str, Any]) -> str | None:
    for field in ("request_id", "approval_id", "job_id", "failure_id", "id"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _workflow_id(workflow_config: Mapping[str, Any]) -> str:
    workflow_id = workflow_config.get("workflow_id")
    return workflow_id if isinstance(workflow_id, str) and workflow_id else "unknown_workflow"


def _int_value(value: object, *, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
