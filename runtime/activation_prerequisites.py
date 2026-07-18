from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


ACTIVATION_PREREQUISITE_FILENAME = "validator_activation_prerequisite.json"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def build_activation_audit_binding(
    *,
    project_root: Path,
    planning_dir: Path,
    workflow_id: str,
    plan_draft_path: Path,
    readiness_report_path: Path,
    readiness_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind an audit to the exact draft, readiness report, and optional prerequisite.

    The prerequisite is opt-in: it is required when the draft names the canonical
    filename or when that file exists in the planner run identified by readiness.
    """

    project = project_root.expanduser().resolve()
    planning = planning_dir.expanduser().resolve()
    errors: list[str] = []
    blocker_codes: list[str] = []
    bindings: dict[str, str] = {}

    try:
        draft_text = plan_draft_path.read_text(encoding="utf-8")
    except OSError:
        draft_text = ""

    planner_run_id = _mapping_text(readiness_report, "run_id")
    prerequisite_path: Path | None = None
    run_id_valid = bool(planner_run_id and RUN_ID_RE.fullmatch(planner_run_id))
    if run_id_valid:
        prerequisite_path = planning / "runs" / planner_run_id / ACTIVATION_PREREQUISITE_FILENAME

    required = ACTIVATION_PREREQUISITE_FILENAME in draft_text or bool(
        prerequisite_path is not None and prerequisite_path.is_file()
    )
    if not required:
        return {
            "required": False,
            "ok": True,
            "errors": [],
            "warnings": [],
            "blocker_codes": [],
            "bindings": {},
            "prerequisite_path": None,
        }

    if not run_id_valid:
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_run_identity_invalid",
            "The readiness report does not identify a safe planner run for the activation prerequisite.",
        )
        return _binding_result(required, errors, blocker_codes, bindings, prerequisite_path)

    assert prerequisite_path is not None
    prerequisite_rel, path_problem = _project_relative_path(project, prerequisite_path)
    if path_problem:
        _problem(errors, blocker_codes, "activation_prerequisite_path_unsafe", path_problem)
    elif not prerequisite_path.is_file():
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_missing",
            f"Required activation prerequisite is missing: {prerequisite_rel}.",
        )
    else:
        record, record_problem = _read_json_object(prerequisite_path)
        if record_problem:
            _problem(errors, blocker_codes, "activation_prerequisite_malformed", record_problem)
        elif record is not None:
            if record.get("workflow_id") != workflow_id:
                _problem(
                    errors,
                    blocker_codes,
                    "activation_prerequisite_workflow_mismatch",
                    "Activation prerequisite workflow_id does not match the active workflow.",
                )
            if record.get("run_id") != planner_run_id:
                _problem(
                    errors,
                    blocker_codes,
                    "activation_prerequisite_run_mismatch",
                    "Activation prerequisite run_id does not match the readiness planner run.",
                )
            if record.get("status") != "ready_for_fresh_audit":
                _problem(
                    errors,
                    blocker_codes,
                    "activation_prerequisite_not_ready",
                    "Activation prerequisite status must be ready_for_fresh_audit.",
                )

    draft_rel, draft_problem = _project_relative_path(project, plan_draft_path)
    readiness_rel, readiness_problem = _project_relative_path(project, readiness_report_path)
    if draft_problem:
        _problem(errors, blocker_codes, "activation_prerequisite_draft_path_unsafe", draft_problem)
    if readiness_problem:
        _problem(errors, blocker_codes, "activation_prerequisite_readiness_path_unsafe", readiness_problem)

    draft_sha = _sha256_file(plan_draft_path)
    readiness_sha = _sha256_file(readiness_report_path)
    prerequisite_sha = _sha256_file(prerequisite_path)
    for label, value in (
        ("plan draft", draft_sha),
        ("readiness report", readiness_sha),
        ("activation prerequisite", prerequisite_sha),
    ):
        if value is None:
            _problem(
                errors,
                blocker_codes,
                "activation_prerequisite_binding_unreadable",
                f"Cannot hash the {label} for the audit binding.",
            )

    if not errors:
        assert draft_rel is not None
        assert readiness_rel is not None
        assert prerequisite_rel is not None
        assert draft_sha is not None
        assert readiness_sha is not None
        assert prerequisite_sha is not None
        bindings = {
            "workflow_id": workflow_id,
            "planner_run_id": planner_run_id,
            "plan_draft_path": draft_rel,
            "plan_draft_sha256": draft_sha,
            "readiness_report_path": readiness_rel,
            "readiness_report_sha256": readiness_sha,
            "activation_prerequisite_path": prerequisite_rel,
            "activation_prerequisite_sha256": prerequisite_sha,
        }

    return _binding_result(required, errors, blocker_codes, bindings, prerequisite_path)


def preflight_activation_prerequisite(
    *,
    project_root: Path,
    planning_dir: Path,
    workflow_id: str,
    plan_draft_path: Path,
    readiness_report_path: Path,
    readiness_report: Mapping[str, Any] | None,
    audit_report_path: Path,
    audit_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate the optional prerequisite and return exact bytes for checkpoint proof."""

    project = project_root.expanduser().resolve()
    binding = build_activation_audit_binding(
        project_root=project,
        planning_dir=planning_dir,
        workflow_id=workflow_id,
        plan_draft_path=plan_draft_path,
        readiness_report_path=readiness_report_path,
        readiness_report=readiness_report,
    )
    if binding["required"] is not True:
        return {
            **binding,
            "expected_checkpoint_hashes": {},
            "required_checkpoint_reason": None,
        }

    errors = [str(item) for item in binding.get("errors", [])]
    blocker_codes = [str(item) for item in binding.get("blocker_codes", [])]
    expected_hashes: dict[str, str] = {}
    prerequisite_path_raw = binding.get("prerequisite_path")
    prerequisite_path = Path(prerequisite_path_raw) if isinstance(prerequisite_path_raw, str) else None
    record: Mapping[str, Any] | None = None
    if prerequisite_path is not None and prerequisite_path.is_file():
        record, record_problem = _read_json_object(prerequisite_path)
        if record_problem and "activation_prerequisite_malformed" not in blocker_codes:
            _problem(errors, blocker_codes, "activation_prerequisite_malformed", record_problem)

    if record is not None:
        _validate_record_contract(
            project=project,
            workflow_id=workflow_id,
            record=record,
            readiness_report=readiness_report,
            audit_report=audit_report,
            audit_report_path=audit_report_path,
            current_bindings=binding.get("bindings", {}),
            prerequisite_path=prerequisite_path,
            expected_hashes=expected_hashes,
            errors=errors,
            blocker_codes=blocker_codes,
        )

    return {
        "required": True,
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": [],
        "blocker_codes": _dedupe(blocker_codes),
        "bindings": dict(binding.get("bindings", {})),
        "prerequisite_path": prerequisite_path.as_posix() if prerequisite_path is not None else None,
        "expected_checkpoint_hashes": dict(sorted(expected_hashes.items())),
        "required_checkpoint_reason": "before_plan_activation",
    }


def verify_activation_checkpoint(
    *,
    project_root: Path,
    checkpoint_result: Mapping[str, Any],
    prerequisite_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove that the managed Git checkpoint contains every preflight-bound byte."""

    if prerequisite_result.get("required") is not True:
        return {"required": False, "ok": True, "errors": [], "blocker_codes": [], "verified_paths": []}

    errors: list[str] = []
    blocker_codes: list[str] = []
    expected = prerequisite_result.get("expected_checkpoint_hashes")
    if not isinstance(expected, Mapping) or not expected:
        _problem(
            errors,
            blocker_codes,
            "activation_checkpoint_expectations_missing",
            "Activation prerequisite produced no checkpoint hash expectations.",
        )
        return _checkpoint_result(errors, blocker_codes, [])

    checkpoint = checkpoint_result.get("checkpoint")
    if checkpoint_result.get("ok") is not True or not isinstance(checkpoint, Mapping):
        _problem(
            errors,
            blocker_codes,
            "activation_checkpoint_unavailable",
            "before_plan_activation checkpoint metadata is unavailable.",
        )
        return _checkpoint_result(errors, blocker_codes, [])

    required_reason = prerequisite_result.get("required_checkpoint_reason")
    if checkpoint.get("reason") != required_reason:
        _problem(
            errors,
            blocker_codes,
            "activation_checkpoint_reason_mismatch",
            f"Activation checkpoint reason must be {required_reason!r}.",
        )

    project = project_root.expanduser().resolve()
    repository_root_raw = checkpoint.get("repository_root")
    repository_root_path = Path(str(repository_root_raw)).expanduser() if repository_root_raw else project
    if not repository_root_path.is_absolute():
        repository_root_path = project / repository_root_path
    repository_root = repository_root_path.resolve()
    try:
        project_prefix = project.relative_to(repository_root)
    except ValueError:
        _problem(
            errors,
            blocker_codes,
            "activation_checkpoint_repository_mismatch",
            "Checkpoint repository_root does not contain the project root.",
        )
        return _checkpoint_result(errors, blocker_codes, [])

    commit = str(checkpoint.get("commit") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        _problem(
            errors,
            blocker_codes,
            "activation_checkpoint_commit_invalid",
            "Checkpoint metadata does not contain a valid commit id.",
        )
        return _checkpoint_result(errors, blocker_codes, [])

    verified: list[str] = []
    for raw_path, raw_expected_hash in sorted(expected.items()):
        relative = _safe_relative_text(raw_path)
        expected_hash = str(raw_expected_hash)
        if relative is None or not SHA256_RE.fullmatch(expected_hash):
            _problem(
                errors,
                blocker_codes,
                "activation_checkpoint_expectation_invalid",
                f"Invalid checkpoint expectation for {raw_path!r}.",
            )
            continue
        tree_path = (project_prefix / PurePosixPath(relative)).as_posix()
        shown = subprocess.run(
            ["git", "-C", str(repository_root), "show", f"{commit}:{tree_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if shown.returncode != 0:
            _problem(
                errors,
                blocker_codes,
                "activation_checkpoint_path_missing",
                f"Checkpoint omits required pinned path: {relative}.",
            )
            continue
        actual_hash = "sha256:" + hashlib.sha256(shown.stdout).hexdigest()
        if actual_hash != expected_hash:
            _problem(
                errors,
                blocker_codes,
                "activation_checkpoint_hash_mismatch",
                f"Checkpoint bytes do not match the activation pin for {relative}.",
            )
            continue
        verified.append(relative)

    return _checkpoint_result(errors, blocker_codes, verified)


def _validate_record_contract(
    *,
    project: Path,
    workflow_id: str,
    record: Mapping[str, Any],
    readiness_report: Mapping[str, Any] | None,
    audit_report: Mapping[str, Any] | None,
    audit_report_path: Path,
    current_bindings: Any,
    prerequisite_path: Path | None,
    expected_hashes: dict[str, str],
    errors: list[str],
    blocker_codes: list[str],
) -> None:
    if record.get("workflow_id") != workflow_id:
        _problem(errors, blocker_codes, "activation_prerequisite_workflow_mismatch", "Prerequisite workflow identity changed.")
    planner_run_id = _mapping_text(readiness_report, "run_id")
    if record.get("run_id") != planner_run_id:
        _problem(errors, blocker_codes, "activation_prerequisite_run_mismatch", "Prerequisite planner run identity changed.")
    if record.get("status") != "ready_for_fresh_audit":
        _problem(errors, blocker_codes, "activation_prerequisite_not_ready", "Prerequisite is not ready_for_fresh_audit.")

    configuration = _mapping(record.get("current_configuration"))
    if configuration.get("validation.validator_agent_mode") != "always":
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_validator_policy_invalid",
            "Prerequisite must record validation.validator_agent_mode=always.",
        )

    runtime = _mapping(record.get("execution_runtime"))
    descriptors: list[tuple[str, Mapping[str, Any]]] = [
        ("workflow configuration", configuration),
        ("validation runtime", _mapping(runtime.get("validation"))),
        ("reconciliation runtime", _mapping(runtime.get("reconciliation"))),
        ("planning runtime", _mapping(runtime.get("planning"))),
        ("activation prerequisite runtime", _mapping(runtime.get("activation_prerequisites"))),
        ("validator fail-closed regression", _mapping(record.get("integration_regression"))),
        ("activation prerequisite regression", _mapping(record.get("activation_preflight_regression"))),
        ("plan draft", _mapping(record.get("plan_draft"))),
    ]
    durability = _mapping(record.get("durability_gate"))
    descriptors.append(
        (
            "version-control configuration",
            {
                "path": durability.get("version_control_config_path"),
                "sha256": durability.get("version_control_config_sha256"),
            },
        )
    )

    for label, descriptor in descriptors:
        _collect_pin(project, label, descriptor, expected_hashes, errors, blocker_codes)

    for test_key in ("integration_regression", "activation_preflight_regression"):
        test_record = _mapping(record.get(test_key))
        if test_record.get("status") != "passed" or test_record.get("exit_code") != 0:
            _problem(
                errors,
                blocker_codes,
                "activation_prerequisite_regression_not_passed",
                f"{test_key} must record a fresh passing exit code.",
            )

    if durability.get("checkpoint_backend") != "managed_refs":
        _problem(errors, blocker_codes, "activation_prerequisite_checkpoint_backend_invalid", "Managed Git refs are required.")
    if durability.get("required_checkpoint_reason") != "before_plan_activation":
        _problem(errors, blocker_codes, "activation_prerequisite_checkpoint_reason_invalid", "before_plan_activation is required.")
    for flag in ("checkpoint_is_fail_closed", "checkpoint_must_precede_plan_write"):
        if durability.get(flag) is not True:
            _problem(errors, blocker_codes, "activation_prerequisite_checkpoint_contract_invalid", f"{flag} must be true.")

    recorded_at = _timestamp(record.get("recorded_at"))
    if recorded_at is None:
        _problem(errors, blocker_codes, "activation_prerequisite_timestamp_invalid", "Prerequisite recorded_at is invalid.")
    for label, report in (("readiness", readiness_report), ("audit", audit_report)):
        generated_at = _timestamp(report.get("generated_at") if isinstance(report, Mapping) else None)
        if recorded_at is None or generated_at is None or generated_at <= recorded_at:
            _problem(
                errors,
                blocker_codes,
                "activation_prerequisite_stale",
                f"The {label} report must be generated after the prerequisite record.",
            )

    expected_bindings = dict(current_bindings) if isinstance(current_bindings, Mapping) else {}
    observed_bindings = audit_report.get("activation_bindings") if isinstance(audit_report, Mapping) else None
    if not expected_bindings or not isinstance(observed_bindings, Mapping) or dict(observed_bindings) != expected_bindings:
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_audit_binding_mismatch",
            "The passing audit is not bound to the current draft, readiness report, and prerequisite bytes.",
        )

    for path_field, hash_field, label in (
        ("plan_draft_path", "plan_draft_sha256", "plan draft"),
        ("readiness_report_path", "readiness_report_sha256", "readiness report"),
        ("activation_prerequisite_path", "activation_prerequisite_sha256", "activation prerequisite"),
    ):
        relative = _safe_relative_text(expected_bindings.get(path_field))
        expected_hash = expected_bindings.get(hash_field)
        if relative is None or not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
            _problem(errors, blocker_codes, "activation_prerequisite_audit_binding_invalid", f"Invalid {label} audit binding.")
        else:
            expected_hashes[relative] = expected_hash

    audit_rel, audit_path_problem = _project_relative_path(project, audit_report_path)
    audit_sha = _sha256_file(audit_report_path)
    if audit_path_problem or audit_rel is None or audit_sha is None:
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_audit_unreadable",
            audit_path_problem or "The canonical audit report cannot be hashed.",
        )
    else:
        expected_hashes[audit_rel] = audit_sha

    if prerequisite_path is not None:
        prerequisite_rel, _ = _project_relative_path(project, prerequisite_path)
        if prerequisite_rel is not None and prerequisite_rel not in expected_hashes:
            prerequisite_sha = _sha256_file(prerequisite_path)
            if prerequisite_sha is not None:
                expected_hashes[prerequisite_rel] = prerequisite_sha

    raw_pinned_paths = durability.get("pinned_paths_must_be_included")
    if not isinstance(raw_pinned_paths, list) or not all(isinstance(item, str) for item in raw_pinned_paths):
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_durability_inventory_invalid",
            "pinned_paths_must_be_included must be a list of project-relative paths.",
        )
        return
    pinned_paths = [_safe_relative_text(item) for item in raw_pinned_paths]
    if any(item is None for item in pinned_paths) or len(set(pinned_paths)) != len(pinned_paths):
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_durability_inventory_invalid",
            "Durability inventory contains an unsafe or duplicate path.",
        )
        return
    pinned_set = {str(item) for item in pinned_paths}
    expected_set = set(expected_hashes)
    missing = sorted(expected_set - pinned_set)
    unbound = sorted(pinned_set - expected_set)
    if missing:
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_durability_path_missing",
            "Durability inventory omits pinned paths: " + ", ".join(missing),
        )
    if unbound:
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_durability_path_unbound",
            "Durability inventory contains paths with no audited hash pin: " + ", ".join(unbound),
        )


def _collect_pin(
    project: Path,
    label: str,
    descriptor: Mapping[str, Any],
    expected_hashes: dict[str, str],
    errors: list[str],
    blocker_codes: list[str],
) -> None:
    relative = _safe_relative_text(descriptor.get("path"))
    expected_hash = descriptor.get("sha256")
    if relative is None or not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
        _problem(
            errors,
            blocker_codes,
            "activation_prerequisite_pin_invalid",
            f"The {label} pin must contain a safe path and sha256 digest.",
        )
        return
    path = (project / PurePosixPath(relative)).resolve()
    try:
        path.relative_to(project)
    except ValueError:
        _problem(errors, blocker_codes, "activation_prerequisite_pin_unsafe", f"The {label} path escapes the project.")
        return
    actual_hash = _sha256_file(path)
    if actual_hash is None:
        _problem(errors, blocker_codes, "activation_prerequisite_pin_missing", f"Pinned {label} is missing: {relative}.")
        return
    if actual_hash != expected_hash:
        _problem(errors, blocker_codes, "activation_prerequisite_hash_drift", f"Pinned {label} hash drifted: {relative}.")
        return
    previous = expected_hashes.get(relative)
    if previous is not None and previous != expected_hash:
        _problem(errors, blocker_codes, "activation_prerequisite_pin_conflict", f"Conflicting hashes pin {relative}.")
        return
    expected_hashes[relative] = expected_hash


def _binding_result(
    required: bool,
    errors: list[str],
    blocker_codes: list[str],
    bindings: Mapping[str, str],
    prerequisite_path: Path | None,
) -> dict[str, Any]:
    return {
        "required": required,
        "ok": not errors,
        "errors": _dedupe(errors),
        "warnings": [],
        "blocker_codes": _dedupe(blocker_codes),
        "bindings": dict(bindings),
        "prerequisite_path": prerequisite_path.as_posix() if prerequisite_path is not None else None,
    }


def _checkpoint_result(errors: list[str], blocker_codes: list[str], verified: list[str]) -> dict[str, Any]:
    return {
        "required": True,
        "ok": not errors,
        "errors": _dedupe(errors),
        "blocker_codes": _dedupe(blocker_codes),
        "verified_paths": verified,
    }


def _read_json_object(path: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, f"{path.as_posix()} is not readable JSON: {error}"
    if not isinstance(value, Mapping):
        return None, f"{path.as_posix()} must contain a JSON object."
    return value, None


def _project_relative_path(project: Path, path: Path) -> tuple[str | None, str | None]:
    try:
        relative = path.expanduser().resolve().relative_to(project)
    except ValueError:
        return None, f"Path escapes the project boundary: {path.as_posix()}."
    safe = _safe_relative_text(relative.as_posix())
    if safe is None:
        return None, f"Path is not a safe project-relative file: {path.as_posix()}."
    return safe, None


def _safe_relative_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mapping_text(value: Mapping[str, Any] | None, key: str) -> str:
    raw = value.get(key) if isinstance(value, Mapping) else None
    return raw if isinstance(raw, str) else ""


def _sha256_file(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _problem(errors: list[str], codes: list[str], code: str, message: str) -> None:
    errors.append(message)
    codes.append(code)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
