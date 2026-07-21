from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from runtime.source_guard import (
    RuntimeSourceDriftError,
    RuntimeSourceGuard,
    capture_runtime_source_snapshot,
    detect_runtime_source_drift,
    read_snapshot_template,
)


class RuntimeSourceGuardTest(unittest.TestCase):
    def test_changed_template_is_detected_before_new_content_is_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "LoopPlane"
            runtime_root = package_root / "runtime"
            schema_root = runtime_root / "schemas"
            template_root = package_root / "templates"
            schema_root.mkdir(parents=True)
            template_root.mkdir(parents=True)
            (runtime_root / "scheduler.py").write_text("VERSION = 1\n", encoding="utf-8")
            (schema_root / "state.schema.json").write_text("{}\n", encoding="utf-8")
            template_path = template_root / "worker.md"
            template_path.write_text("old template\n", encoding="utf-8")
            snapshot = capture_runtime_source_snapshot(package_root)

            self.assertEqual(
                read_snapshot_template(template_path, snapshot=snapshot),
                "old template\n",
            )
            template_path.write_text(
                "new template with a new required variable\n",
                encoding="utf-8",
            )

            drift = detect_runtime_source_drift(snapshot)
            self.assertIsNotNone(drift)
            assert drift is not None
            self.assertIn("templates/worker.md", drift["changed_files"])
            with self.assertRaises(RuntimeSourceDriftError) as raised:
                read_snapshot_template(template_path, snapshot=snapshot)
            self.assertEqual(
                raised.exception.drift["reason"],
                "template_changed_after_process_start",
            )
            guard = RuntimeSourceGuard(snapshot, check_interval_seconds=60)
            self.assertIsNotNone(guard.poll(force=True))

    def test_runtime_python_change_updates_source_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "LoopPlane"
            runtime_root = package_root / "runtime"
            runtime_root.mkdir(parents=True)
            source_path = runtime_root / "scheduler.py"
            source_path.write_text("VERSION = 1\n", encoding="utf-8")
            snapshot = capture_runtime_source_snapshot(package_root)

            source_path.write_text("VERSION = 200\n", encoding="utf-8")

            drift = detect_runtime_source_drift(snapshot)
            self.assertIsNotNone(drift)
            assert drift is not None
            self.assertNotEqual(
                drift["baseline_fingerprint"],
                drift["current_fingerprint"],
            )
            self.assertEqual(drift["changed_files"], ["runtime/scheduler.py"])

    def test_same_size_change_with_preserved_mtime_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "LoopPlane"
            runtime_root = package_root / "runtime"
            runtime_root.mkdir(parents=True)
            source_path = runtime_root / "scheduler.py"
            source_path.write_text("VALUE = 'AAAA'\n", encoding="utf-8")
            snapshot = capture_runtime_source_snapshot(package_root)
            original_stat = source_path.stat()

            source_path.write_text("VALUE = 'BBBB'\n", encoding="utf-8")
            os.utime(
                source_path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )

            drift = detect_runtime_source_drift(snapshot)
            self.assertIsNotNone(drift)
            assert drift is not None
            self.assertEqual(drift["changed_files"], ["runtime/scheduler.py"])
            self.assertNotEqual(
                drift["baseline_fingerprint"],
                drift["current_fingerprint"],
            )

    def test_metadata_only_touch_does_not_change_content_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "LoopPlane"
            runtime_root = package_root / "runtime"
            runtime_root.mkdir(parents=True)
            source_path = runtime_root / "scheduler.py"
            source_path.write_text("VERSION = 1\n", encoding="utf-8")
            snapshot = capture_runtime_source_snapshot(package_root)

            stat = source_path.stat()
            os.utime(
                source_path,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
            )

            self.assertIsNone(detect_runtime_source_drift(snapshot))

    def test_immutable_snapshot_guard_can_observe_mutable_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            immutable = root / "snapshot"
            checkout = root / "checkout"
            for package_root in (immutable, checkout):
                (package_root / "runtime").mkdir(parents=True)
                (package_root / "templates").mkdir(parents=True)
                (package_root / "runtime" / "scheduler.py").write_text(
                    "VERSION = 1\n",
                    encoding="utf-8",
                )
                (package_root / "templates" / "worker.md").write_text(
                    "generation one\n",
                    encoding="utf-8",
                )
            baseline = capture_runtime_source_snapshot(immutable)
            guard = RuntimeSourceGuard(
                baseline,
                observed_package_root=checkout,
                check_interval_seconds=0,
            )

            self.assertIsNone(guard.poll(force=True))
            (checkout / "runtime" / "scheduler.py").write_text(
                "VERSION = 2\n",
                encoding="utf-8",
            )

            drift = guard.poll(force=True)
            self.assertIsNotNone(drift)
            assert drift is not None
            self.assertEqual(drift["changed_files"], ["runtime/scheduler.py"])


if __name__ == "__main__":
    unittest.main()
