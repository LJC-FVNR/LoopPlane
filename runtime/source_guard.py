from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = PACKAGE_ROOT / "runtime"
TEMPLATE_ROOT = PACKAGE_ROOT / "templates"
SCHEMA_ROOT = RUNTIME_ROOT / "schemas"
DEFAULT_SOURCE_CHECK_INTERVAL_SECONDS = 2.0


class RuntimeSourceDriftError(RuntimeError):
    def __init__(self, drift: Mapping[str, Any]) -> None:
        self.drift = dict(drift)
        changed = self.drift.get("changed_files")
        changed_text = ", ".join(str(item) for item in changed or []) or "runtime source files"
        super().__init__(f"LoopPlane runtime source changed after process startup: {changed_text}")


@dataclass(frozen=True)
class SourceFileRecord:
    path: str
    size_bytes: int
    mtime_ns: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class RuntimeSourceSnapshot:
    package_root: Path
    fingerprint: str
    files: tuple[SourceFileRecord, ...]
    template_contents: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_root": self.package_root.as_posix(),
            "fingerprint": self.fingerprint,
            "file_count": len(self.files),
        }


def capture_runtime_source_snapshot(package_root: Path | str = PACKAGE_ROOT) -> RuntimeSourceSnapshot:
    root = Path(package_root).expanduser().resolve()
    records: list[SourceFileRecord] = []
    template_contents: dict[str, str] = {}
    for path in _tracked_source_paths(root):
        # Read bytes and metadata from the same open file description.  A deploy
        # may atomically replace ``path`` while a process is taking its startup
        # snapshot; mixing bytes from one inode with stat data from another can
        # otherwise create a baseline that no later poll can reason about.
        try:
            with path.open("rb") as handle:
                raw = handle.read()
                stat = os.fstat(handle.fileno())
        except FileNotFoundError:
            # A concurrent atomic deployment may remove an enumerated path.
            # Treat it as absent from this snapshot; the path-set/content hash
            # comparison will report the transition without crashing the guard.
            continue
        relative = path.relative_to(root).as_posix()
        records.append(
            SourceFileRecord(
                path=relative,
                size_bytes=len(raw),
                mtime_ns=stat.st_mtime_ns,
                sha256="sha256:" + sha256(raw).hexdigest(),
            )
        )
        if relative.startswith("templates/"):
            template_contents[relative] = raw.decode("utf-8")
    records.sort(key=lambda record: record.path)
    return RuntimeSourceSnapshot(
        package_root=root,
        fingerprint=_records_fingerprint(records),
        files=tuple(records),
        template_contents=template_contents,
    )


def detect_runtime_source_drift(
    baseline: RuntimeSourceSnapshot,
    *,
    observed_package_root: Path | str | None = None,
    max_changed_files: int = 20,
) -> dict[str, Any] | None:
    # Size and mtime are only cache hints, not content identity.  Tools such as
    # ``rsync -a`` and archive restoration can legitimately replace a file while
    # preserving both values.  Hash on every bounded guard poll so such a source
    # update cannot leave an old supervisor running indefinitely.
    current = capture_runtime_source_snapshot(
        baseline.package_root
        if observed_package_root is None
        else observed_package_root
    )
    if current.fingerprint == baseline.fingerprint:
        return None
    baseline_by_path = {record.path: record for record in baseline.files}
    current_by_path = {record.path: record for record in current.files}
    changed = sorted(
        path
        for path in set(baseline_by_path).union(current_by_path)
        if _record_content_identity(baseline_by_path.get(path))
        != _record_content_identity(current_by_path.get(path))
    )
    return {
        "baseline_fingerprint": baseline.fingerprint,
        "current_fingerprint": current.fingerprint,
        "changed_files": changed[: max(1, int(max_changed_files))],
        "changed_file_count": len(changed),
        "baseline_file_count": len(baseline.files),
        "current_file_count": len(current.files),
    }


class RuntimeSourceGuard:
    def __init__(
        self,
        baseline: RuntimeSourceSnapshot,
        *,
        observed_package_root: Path | str | None = None,
        check_interval_seconds: float = DEFAULT_SOURCE_CHECK_INTERVAL_SECONDS,
    ) -> None:
        self.baseline = baseline
        self.observed_package_root = (
            baseline.package_root
            if observed_package_root is None
            else Path(observed_package_root).expanduser().resolve()
        )
        self.check_interval_seconds = max(0.0, float(check_interval_seconds))
        self._last_checked_monotonic: float | None = None

    def poll(self, *, force: bool = False) -> dict[str, Any] | None:
        now = time.monotonic()
        if (
            not force
            and self._last_checked_monotonic is not None
            and now - self._last_checked_monotonic < self.check_interval_seconds
        ):
            return None
        self._last_checked_monotonic = now
        return detect_runtime_source_drift(
            self.baseline,
            observed_package_root=self.observed_package_root,
        )


def read_snapshot_template(
    path: Path | str,
    *,
    snapshot: RuntimeSourceSnapshot,
) -> str:
    resolved = Path(path).expanduser().resolve()
    try:
        relative = resolved.relative_to(snapshot.package_root).as_posix()
    except ValueError as error:
        raise RuntimeSourceDriftError(
            {
                "baseline_fingerprint": snapshot.fingerprint,
                "current_fingerprint": None,
                "changed_files": [resolved.as_posix()],
                "changed_file_count": 1,
                "reason": "template_outside_runtime_snapshot",
            }
        ) from error
    content = snapshot.template_contents.get(relative)
    if content is None:
        raise RuntimeSourceDriftError(
            {
                "baseline_fingerprint": snapshot.fingerprint,
                "current_fingerprint": None,
                "changed_files": [relative],
                "changed_file_count": 1,
                "reason": "template_missing_from_runtime_snapshot",
            }
        )
    baseline_record = next((record for record in snapshot.files if record.path == relative), None)
    try:
        with resolved.open("rb") as handle:
            raw = handle.read()
            stat = os.fstat(handle.fileno())
    except OSError as error:
        raise RuntimeSourceDriftError(
            {
                "baseline_fingerprint": snapshot.fingerprint,
                "current_fingerprint": None,
                "changed_files": [relative],
                "changed_file_count": 1,
                "reason": f"template_unavailable:{type(error).__name__}",
            }
        ) from error
    current_record = SourceFileRecord(
        path=relative,
        size_bytes=len(raw),
        mtime_ns=stat.st_mtime_ns,
        sha256="sha256:" + sha256(raw).hexdigest(),
    )
    if _record_content_identity(baseline_record) != _record_content_identity(current_record):
        raise RuntimeSourceDriftError(
            {
                "baseline_fingerprint": snapshot.fingerprint,
                "current_fingerprint": None,
                "changed_files": [relative],
                "changed_file_count": 1,
                "reason": "template_changed_after_process_start",
            }
        )
    return content


def read_process_template(path: Path | str) -> str:
    return read_snapshot_template(path, snapshot=PROCESS_RUNTIME_SOURCE_SNAPSHOT)


def _tracked_source_paths(package_root: Path) -> list[Path]:
    runtime_root = package_root / "runtime"
    template_root = package_root / "templates"
    paths = [path for path in runtime_root.rglob("*.py") if path.is_file()]
    schema_root = runtime_root / "schemas"
    if schema_root.is_dir():
        paths.extend(path for path in schema_root.rglob("*.json") if path.is_file())
    if template_root.is_dir():
        paths.extend(path for path in template_root.rglob("*") if path.is_file())
    return sorted(dict.fromkeys(path.resolve() for path in paths))


def _records_fingerprint(records: Sequence[SourceFileRecord]) -> str:
    encoded = json.dumps(
        [
            {
                "path": record.path,
                "sha256": record.sha256,
            }
            for record in records
        ],
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _record_content_identity(record: SourceFileRecord | None) -> tuple[str, str] | None:
    if record is None:
        return None
    return record.path, record.sha256


PROCESS_RUNTIME_SOURCE_SNAPSHOT = capture_runtime_source_snapshot()
