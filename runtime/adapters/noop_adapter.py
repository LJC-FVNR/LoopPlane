from __future__ import annotations

from runtime.adapters.base import (
    DOCTOR_STATUS_OK,
    AdapterDoctorResult,
    AdapterInput,
    AdapterOutput,
    AgentAdapter,
    discover_adapter_produced_files,
    snapshot_adapter_files,
    utc_timestamp,
    write_adapter_input,
    write_adapter_result,
)
from runtime.runner_locks import acquire_runner_resource_lock, with_runner_resource_lock_metadata


class NoopAdapter(AgentAdapter):
    adapter_name = "noop"

    def run(self, adapter_input: AdapterInput) -> AdapterOutput:
        adapter_input.ensure_run_dirs()
        paths = adapter_input.output_paths()
        adapter_input_path = write_adapter_input(adapter_input)
        pre_run_files = snapshot_adapter_files(adapter_input)
        started_at = utc_timestamp()

        with acquire_runner_resource_lock(adapter_input) as runner_lock:
            paths.stdout_path.write_text(
                f"noop adapter accepted run {adapter_input.run_id}\n",
                encoding="utf-8",
            )
            paths.stderr_path.write_text("", encoding="utf-8")
            paths.final_output_path.write_text(
                "\n".join(
                    (
                        "# Noop Adapter Result",
                        "",
                        f"Run: {adapter_input.run_id}",
                        f"Runner: {adapter_input.runner_id}",
                        f"Role: {adapter_input.role}",
                        "External execution: false",
                        "",
                    )
                ),
                encoding="utf-8",
            )

            ended_at = utc_timestamp()
            produced_files = discover_adapter_produced_files(
                adapter_input,
                before=pre_run_files,
                explicit=(
                    adapter_input_path,
                    paths.stdout_path,
                    paths.stderr_path,
                    paths.final_output_path,
                    paths.adapter_result_path,
                ),
            )
            output = AdapterOutput.from_input(
                adapter_input,
                started_at=started_at,
                ended_at=ended_at,
                exit_code=0,
                produced_files=produced_files,
                adapter_metadata=with_runner_resource_lock_metadata(
                    {
                        "external_execution": False,
                        "delivery_mode": adapter_input.prompt_delivery.get("mode"),
                        "prompt_bytes": len(adapter_input.prompt_content.encode("utf-8")),
                    },
                    runner_lock,
                ),
            )
            write_adapter_result(output)
            return output

    def doctor(self, adapter_input: AdapterInput) -> AdapterDoctorResult:
        return AdapterDoctorResult.ok(
            adapter_input,
            message="Noop adapter is available.",
            checks=(
                {
                    "name": "external_execution",
                    "status": DOCTOR_STATUS_OK,
                    "message": "No external command is required.",
                },
            ),
            adapter_metadata={"external_execution": False},
        )
