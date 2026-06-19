from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.adapters.base import (
    ADAPTER_RESULT_FILENAME,
    DOCTOR_STATUS_WAITING_CONFIG,
    AdapterContractError,
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
    AgentAdapter,
    utc_timestamp,
    write_adapter_result,
)
from runtime.agent_runners import RunnerConfig


def runner_config() -> RunnerConfig:
    return RunnerConfig(
        runner_id="worker",
        role="worker",
        adapter="codex_cli",
        command="codex",
        cwd=".",
        prompt_delivery={"mode": "file_argument"},
        args=("--profile", "loopplane"),
        env={"LOOPPLANE_ROLE": "worker"},
        timeout_seconds=3600,
        stream_logs=True,
        permission_policy={
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": True,
            "read_only": False,
        },
        doctor={"check_command": "codex --version", "requires_auth": True},
        enabled=True,
    )


class UnimplementedDoctorAdapter(AgentAdapter):
    adapter_name = "demo"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        raise NotImplementedError


class AdapterBaseContractTest(unittest.TestCase):
    def test_input_from_runner_config_contains_prompt_runner_dirs_timeout_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Do the task.\n", encoding="utf-8")

            adapter_input = AdapterInput.from_runner_config(
                run_id="run_20260610_000001",
                workflow_id="wf_20260610_000001",
                runner_config=runner_config(),
                prompt_path=prompt_path,
                scheduler_run_dir=root / ".loopplane/runtime/runs/run_20260610_000001",
                role_output_dir=root / ".loopplane/results/T001/runs/run_20260610_000001",
                task_id="T001",
                task_evidence_run_dir=root / ".loopplane/results/T001/runs/run_20260610_000001",
                env={"EXTRA": "1"},
            )

            data = adapter_input.to_dict()

            self.assertEqual(data["schema_version"], "1.5")
            self.assertEqual(data["run_id"], "run_20260610_000001")
            self.assertEqual(data["workflow_id"], "wf_20260610_000001")
            self.assertEqual(data["runner_id"], "worker")
            self.assertEqual(data["role"], "worker")
            self.assertEqual(data["task_id"], "T001")
            self.assertEqual(data["prompt_path"], prompt_path.as_posix())
            self.assertEqual(data["prompt_content"], "Do the task.\n")
            self.assertIn(".loopplane/runtime/runs/run_20260610_000001", data["scheduler_run_dir"])
            self.assertIn(".loopplane/results/T001/runs/run_20260610_000001", data["role_output_dir"])
            self.assertIn(".loopplane/results/T001/runs/run_20260610_000001", data["task_evidence_run_dir"])
            self.assertEqual(data["cwd"], ".")
            self.assertEqual(data["adapter"], "codex_cli")
            self.assertEqual(data["command"], "codex")
            self.assertEqual(data["args"], ["--profile", "loopplane"])
            self.assertEqual(data["env"], {"LOOPPLANE_ROLE": "worker", "EXTRA": "1"})
            self.assertEqual(data["timeout_seconds"], 3600)
            self.assertTrue(data["permission_policy"]["allow_command_execution"])
            self.assertEqual(data["prompt_delivery"], {"mode": "file_argument"})
            self.assertEqual(data["runner_config"]["adapter"], "codex_cli")

    def test_input_json_round_trip_and_non_task_null_evidence_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Plan the project.\n", encoding="utf-8")
            source = AdapterInput.from_runner_config(
                run_id="run_planner",
                workflow_id="wf",
                runner_config=runner_config(),
                prompt_path=prompt_path,
                scheduler_run_dir=root / "runtime/run_planner",
                role_output_dir=root / "planning/run_planner",
                task_id=None,
                task_evidence_run_dir=None,
                prompt_content="Plan the project.\n",
            )
            json_path = root / "adapter_input.json"

            written_path = source.write_json(json_path)
            loaded = AdapterInput.read_json(written_path)

            self.assertEqual(loaded.to_dict(), source.to_dict())
            self.assertIsNone(loaded.task_id)
            self.assertIsNone(loaded.task_evidence_run_dir)

    def test_input_env_preserves_selected_role_over_inherited_runner_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Plan the project.\n", encoding="utf-8")

            adapter_input = AdapterInput.from_runner_config(
                run_id="run_planner",
                workflow_id="wf",
                runner_config=runner_config(),
                prompt_path=prompt_path,
                scheduler_run_dir=root / "runtime/run_planner",
                role_output_dir=root / "planning/run_planner",
                task_id=None,
                task_evidence_run_dir=None,
                prompt_content="Plan the project.\n",
                role="planner",
            )

            self.assertEqual(adapter_input.role, "planner")
            self.assertEqual(adapter_input.env["LOOPPLANE_ROLE"], "planner")

    def test_run_dir_and_output_helpers_write_adapter_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Run.\n", encoding="utf-8")
            adapter_input = AdapterInput.from_runner_config(
                run_id="run_worker",
                workflow_id="wf",
                runner_config=runner_config(),
                prompt_path=prompt_path,
                scheduler_run_dir=root / "runtime/run_worker",
                role_output_dir=root / "results/T001/run_worker",
                task_id="T001",
                task_evidence_run_dir=root / "results/T001/run_worker",
            )

            adapter_input.ensure_run_dirs()
            paths = adapter_input.output_paths()
            output = AdapterOutput.from_input(
                adapter_input,
                started_at="2026-06-10T12:00:00Z",
                ended_at="2026-06-10T12:01:00Z",
                exit_code=0,
                output_paths=paths,
                produced_files=(adapter_input.task_evidence_run_dir / "agent_status.json",),
                adapter_metadata={"delivery_mode": adapter_input.prompt_delivery["mode"]},
            )
            result_path = write_adapter_result(output)

            self.assertTrue(adapter_input.scheduler_run_dir.is_dir())
            self.assertTrue(adapter_input.role_output_dir.is_dir())
            self.assertTrue(adapter_input.task_evidence_run_dir.is_dir())
            self.assertEqual(result_path, adapter_input.scheduler_run_dir / ADAPTER_RESULT_FILENAME)
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result_data["exit_code"], 0)
            self.assertFalse(result_data["timed_out"])
            self.assertEqual(result_data["stdout_path"], paths.stdout_path.as_posix())
            self.assertEqual(result_data["stderr_path"], paths.stderr_path.as_posix())
            self.assertEqual(result_data["final_output_path"], paths.final_output_path.as_posix())
            self.assertEqual(result_data["adapter_result_path"], result_path.as_posix())
            self.assertEqual(
                result_data["produced_files"],
                [(adapter_input.task_evidence_run_dir / "agent_status.json").as_posix()],
            )
            self.assertEqual(result_data["adapter_metadata"], {"delivery_mode": "file_argument"})
            self.assertEqual(AdapterOutput.read_json(result_path).to_dict(), output.to_dict())

    def test_contract_validation_rejects_missing_required_output_fields(self) -> None:
        with self.assertRaisesRegex(AdapterContractError, "stderr_path"):
            AdapterOutput.from_dict(
                {
                    "schema_version": "1.5",
                    "run_id": "run_worker",
                    "runner_id": "worker",
                    "role": "worker",
                    "adapter": "codex_cli",
                    "command": "codex",
                    "cwd": ".",
                    "started_at": "2026-06-10T12:00:00Z",
                    "ended_at": "2026-06-10T12:01:00Z",
                    "exit_code": 1,
                    "timed_out": False,
                    "stdout_path": "stdout.log",
                    "final_output_path": "final.md",
                    "adapter_result_path": "adapter_result.json",
                    "produced_files": [],
                    "adapter_metadata": {},
                }
            )

    def test_doctor_failures_use_waiting_config_not_business_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Inspect config.\n", encoding="utf-8")
            adapter_input = AdapterInput.from_runner_config(
                run_id="run_doctor",
                workflow_id="wf",
                runner_config=runner_config(),
                prompt_path=prompt_path,
                scheduler_run_dir=root / "runtime/run_doctor",
                role_output_dir=root / "results/run_doctor",
            )

            result = AdapterDoctorResult.waiting_config(
                adapter_input,
                checks=(
                    {
                        "name": "command_exists",
                        "status": DOCTOR_STATUS_WAITING_CONFIG,
                        "message": "codex was not found",
                    },
                ),
                message="Runner command is unavailable.",
            )
            default_result = UnimplementedDoctorAdapter().doctor(adapter_input)

            self.assertEqual(result.to_dict()["status"], "waiting_config")
            self.assertIn("command is unavailable", result.to_dict()["message"])
            self.assertEqual(default_result.to_dict()["status"], "waiting_config")

    def test_utc_timestamp_uses_zulu_time(self) -> None:
        self.assertRegex(utc_timestamp(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


if __name__ == "__main__":
    unittest.main()
