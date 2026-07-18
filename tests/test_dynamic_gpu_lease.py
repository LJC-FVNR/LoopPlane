from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "loopplane-gpu-lease"
LOADER = importlib.machinery.SourceFileLoader("loopplane_gpu_lease", str(SCRIPT))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None and SPEC.loader is not None
gpu_lease = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gpu_lease
SPEC.loader.exec_module(gpu_lease)


class DynamicGpuLeaseTest(unittest.TestCase):
    def test_parses_gpu_and_compute_process_tables(self) -> None:
        gpus = gpu_lease.parse_gpus("0, GPU-a\n3, GPU-d\n")
        self.assertEqual([(gpu.index, gpu.uuid) for gpu in gpus], [(0, "GPU-a"), (3, "GPU-d")])
        self.assertEqual(
            gpu_lease.parse_compute_processes("GPU-a, 123\nGPU-a, 456\nGPU-d, 789\n"),
            {"GPU-a": [123, 456], "GPU-d": [789]},
        )

    def test_renders_runtime_selected_device_without_static_binding(self) -> None:
        command = gpu_lease.render_command(
            ["worker", "--physical-device", "{gpu_index}", "--uuid={gpu_uuid}"],
            gpu_lease.Gpu(index=5, uuid="GPU-five"),
        )
        self.assertEqual(command, ["worker", "--physical-device", "5", "--uuid=GPU-five"])

    def test_shared_lock_prevents_two_workflows_from_claiming_same_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            gpu = gpu_lease.Gpu(index=3, uuid="GPU-shared")
            first = gpu_lease.try_lock(lock_dir, gpu)
            self.assertIsNotNone(first)
            assert first is not None
            first_handle, _ = first
            try:
                self.assertIsNone(gpu_lease.try_lock(lock_dir, gpu))
            finally:
                fcntl.flock(first_handle.fileno(), fcntl.LOCK_UN)
                first_handle.close()
            second = gpu_lease.try_lock(lock_dir, gpu)
            self.assertIsNotNone(second)
            assert second is not None
            second[0].close()

    def test_rejected_quiet_window_releases_existing_lock_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_dir = Path(tmp)
            gpu = gpu_lease.Gpu(index=3, uuid="GPU-shared")
            lock_dir.mkdir(parents=True, exist_ok=True)
            (lock_dir / "GPU-shared.lock").write_text('{"previous": true}\n', encoding="utf-8")
            with (
                mock.patch.object(gpu_lease, "query_gpus", return_value=[gpu]),
                mock.patch.object(
                    gpu_lease,
                    "query_compute_processes",
                    side_effect=[{}, {}, {"GPU-shared": [99]}],
                ),
                self.assertRaises(TimeoutError),
            ):
                gpu_lease.wait_for_lease(
                    nvidia_smi="unused",
                    lock_dir=lock_dir,
                    owner="test-owner",
                    poll_seconds=0,
                    quiet_seconds=0,
                    timeout_seconds=0,
                )
            acquired = gpu_lease.try_lock(lock_dir, gpu)
            self.assertIsNotNone(acquired)
            assert acquired is not None
            acquired[0].close()


if __name__ == "__main__":
    unittest.main()
