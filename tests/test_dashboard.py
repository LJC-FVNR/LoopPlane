from __future__ import annotations

import base64
import gzip
import json
import os
import re
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from hashlib import sha256
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import runtime.dashboard as dashboard_module
from runtime.dashboard import (
    _cmdline_is_loopplane_dashboard,
    _render_graph_panel,
    _render_markdown_document,
    _render_plan_panel,
    _status_pill,
    render_static_dashboard,
)
from runtime.loopplane_home import loopplane_home_layout
from runtime.change_requests import submit_change_request
from runtime.control import record_control_request
from runtime.init_workflow import init_project
from runtime.path_resolution import WorkflowPaths, load_workflow_config
from runtime.read_models import READ_MODEL_FILES, rebuild_read_models
from runtime.reconciliation import run_reconciler
from runtime.scheduler import append_event, run_scheduler
from runtime.validation import run_validator
from tests.test_inspector import configure_fake_inspector
from tests.test_human_summaries import configure_fake_summary_agent
from tests.test_read_models import write_ready_plan_draft
from tests.test_validation import write_plan, write_worker_run


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
DASHBOARD_CSS = REPO_ROOT / "dashboard" / "public" / "static_dashboard.css"
DASHBOARD_JS = REPO_ROOT / "dashboard" / "public" / "static_dashboard.js"


def dashboard_script_document(html: str) -> dict[str, object]:
    match = re.search(r'<script id="loopplane-read-models" type="application/json">(.+?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1))


def dashboard_script_payload(html: str) -> dict[str, object]:
    script_payload = dashboard_script_document(html)
    if script_payload.get("payload_encoding") != "gzip+base64":
        return script_payload
    compressed = base64.b64decode(str(script_payload["payload_compressed"]))
    return json.loads(gzip.decompress(compressed).decode("utf-8"))


def file_hashes(project: Path, paths: list[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        hashes[path.relative_to(project).as_posix()] = sha256(path.read_bytes()).hexdigest()
    return hashes


def prepare_dashboard_project(project: Path) -> tuple[WorkflowPaths, Path]:
    init_project(project, "Static dashboard smoke.")
    configure_fake_inspector(project)
    configure_fake_summary_agent(project)
    write_plan(project, validation="file_exists: artifacts/result.txt; command_exit_code: 0")
    workflow_config = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow_config)
    run_dir = write_worker_run(project, create_artifact=True)
    (run_dir / "artifacts" / "scorecard.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40"><text x="4" y="24">score</text></svg>\n',
        encoding="utf-8",
    )
    (run_dir / "prompt.md").write_text(
        "# Worker Prompt\n\nCreate the result artifact and record dashboard node detail evidence.\n",
        encoding="utf-8",
    )
    (run_dir / "final.md").write_text(
        "# Worker Final Output\n\nThe result artifact was produced and validated.\n",
        encoding="utf-8",
    )
    (run_dir / "logs" / "stdout.log").write_text(
        "ok\nSECRET=super-secret-token\nBearer abcdefghijklmnop\n",
        encoding="utf-8",
    )
    (run_dir / "logs" / "stderr.log").write_text("warning: dashboard fixture stderr\n", encoding="utf-8")
    status_path = run_dir / "agent_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["project_changes"] = [
        {"path": "src/app.py", "change_type": "modified", "summary": "Updated dashboard fixture."},
        {"path": ".git/config", "change_type": "modified", "summary": "Unsafe path should be redacted."},
    ]
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "git" / "changed_files.json").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "workflow_id": workflow_config["workflow_id"],
                "task_id": "T001",
                "run_id": "run_fixture",
                "base_commit": "abc123",
                "current_tree": "def456",
                "changed_files": [
                    {"path": "src/app.py", "change_type": "modified", "lines_added": 2, "lines_deleted": 1},
                    {"path": ".git/config", "change_type": "modified", "lines_added": 1, "lines_deleted": 0},
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint_path = paths.runtime_dir / "git_checkpoints.jsonl"
    with checkpoint_path.open("a", encoding="utf-8") as handle:
        for record in (
            {
                "schema_version": "1.5",
                "workflow_id": workflow_config["workflow_id"],
                "checkpoint_id": "cp_before_run_fixture",
                "created_at": "2026-06-11T00:00:00Z",
                "reason": "before_worker_run",
                "task_id": "T001",
                "run_id": "run_fixture",
                "status": "created",
                "provider": "git",
                "backend": "worktree",
                "commit": "abc123",
                "ref": "refs/loopplane/checkpoints/cp_before_run_fixture",
                "repository_root": (project / ".git").as_posix(),
            },
            {
                "schema_version": "1.5",
                "workflow_id": workflow_config["workflow_id"],
                "checkpoint_id": "cp_after_run_fixture",
                "created_at": "2026-06-11T00:01:00Z",
                "reason": "after_validation_pass",
                "task_id": "T001",
                "run_id": "run_fixture",
                "status": "created",
                "provider": "git",
                "backend": "worktree",
                "commit": "def456",
                "ref": "refs/loopplane/checkpoints/cp_after_run_fixture",
                "repository_root": (project / ".git").as_posix(),
            },
        ):
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    validation = run_validator(project, task_id="T001", run_dir=run_dir)
    reconciliation = run_reconciler(project, task_id="T001", run_dir=run_dir)
    if validation["status"] != "pass":
        raise AssertionError(json.dumps(validation, indent=2, sort_keys=True))
    if not reconciliation["ok"]:
        raise AssertionError(json.dumps(reconciliation, indent=2, sort_keys=True))
    append_event(
        paths,
        workflow_id=workflow_config["workflow_id"],
        event_type="dashboard_smoke_tail",
        data={"task_id": "T001", "run_id": "run_fixture"},
        snapshot_interval=None,
    )
    rebuild = rebuild_read_models(project)
    if not rebuild["ok"]:
        raise AssertionError(json.dumps(rebuild, indent=2, sort_keys=True))
    return paths, run_dir


def set_approval_enabled(project: Path, enabled: bool) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["approval"]["enabled"] = enabled
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_approval_required_plan(project: Path) -> None:
    workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
    plan = f"""# Project Plan

## Metadata

- workflow_id: {workflow["workflow_id"]}
- plan_version: 1
- generated_from: PROJECT_BRIEF.md
- active: true

## Phase P0: Approval Fixture

- [ ] T001: Run approval-gated task
  - acceptance: Approval-gated task acceptance.
  - evidence: .loopplane/results/T001/
  - latest: .loopplane/results/T001/latest.json
  - depends_on: []
  - risk: high
  - validation: human_approval: approval required
  - max_attempts: 3
  - approval: required
  - deliverables: approval-gated output.
"""
    (project / "PLAN.md").write_text(plan, encoding="utf-8")


def enable_approvals(project: Path) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["approval"]["enabled"] = True
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def set_dashboard_trusted_local(project: Path, enabled: bool) -> None:
    security_path = project / ".loopplane" / "config" / "security.json"
    security = json.loads(security_path.read_text(encoding="utf-8"))
    security["dashboard"]["trusted_local_mode"] = enabled
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_runner_boundary_fixture(project: Path, *, command: str = "/home/alice/.local/bin/codex") -> None:
    runners_path = project / ".loopplane" / "config" / "agent_runners.json"
    runners = json.loads(runners_path.read_text(encoding="utf-8"))
    worker = runners["runners"]["worker"]
    worker["command"] = command
    worker["env"] = {"API_KEY": "runner-secret-123456"}
    worker["doctor"]["check_command"] = f"{command} --version"
    runners_path.write_text(json.dumps(runners, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_pending_approval(
    project: Path,
    workflow_id: str,
    approval_id: str,
    *,
    task_id: str = "T001",
    message: str = "Approve dashboard API fixture.",
    **extra: object,
) -> None:
    request = {
        "schema_version": "1.5",
        "approval_id": approval_id,
        "requested_at": "2026-06-11T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "workflow_id": workflow_id,
        "task_id": task_id,
        "type": "task_execution",
        "scope": f"{task_id} only",
        "status": "pending",
        "message": message,
    }
    request.update(extra)
    approval_path = project / ".loopplane" / "runtime" / "human_approval_requests.jsonl"
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    with approval_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\n")


def write_canonical_read_models(project: Path, workflow_id: str) -> Path:
    root = project / ".loopplane" / "workflows" / workflow_id
    read_models = root / "read_models"
    read_models.mkdir(parents=True, exist_ok=True)
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "requests").mkdir(parents=True, exist_ok=True)
    (root / "planning").mkdir(parents=True, exist_ok=True)
    (root / "results").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "PLAN.md").write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- active: false

## Phase P0: Canonical

- [ ] C001: Canonical task
  - acceptance: Canonical dashboard API fixture.
  - evidence: .loopplane/workflows/{workflow_id}/results/C001/
  - latest: .loopplane/workflows/{workflow_id}/results/C001/latest.json
  - depends_on: []
  - risk: low
  - validation: manual
""",
        encoding="utf-8",
    )
    (root / "runtime" / "state.json").write_text(
        json.dumps({"schema_version": "1.5", "workflow_id": workflow_id, "status": "archived_view"}, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    workflow_status = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "status": "archived_view",
        "phase": "review",
        "progress": {"total_tasks": 1, "completed_tasks": 0, "blocked_tasks": 0, "progress_percent": 0.0},
        "generated_at": "2026-06-11T00:00:00Z",
        "last_event_seq": 0,
        "source_hashes": {"events_count": 0},
    }
    plan_index = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "phases": [
            {
                "phase_id": "P0",
                "title": "Canonical",
                "status": "pending",
                "tasks": [{"task_id": "C001", "title": "Canonical task", "status": "pending"}],
            }
        ],
        "generated_at": "2026-06-11T00:00:00Z",
        "last_event_seq": 0,
        "source_hashes": {"events_count": 0},
    }
    workflow_graph = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "nodes": [
            {
                "node_id": "run_canonical",
                "type": "worker_run",
                "title": "Canonical run",
                "status": "pass",
                "run_id": "run_canonical",
            }
        ],
        "edges": [],
        "generated_at": "2026-06-11T00:00:00Z",
        "last_event_seq": 0,
        "source_hashes": {"events_count": 0},
    }
    metrics = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "counts": {"tasks_total": 1, "runs_total": 1, "validations_failed": 0},
        "generated_at": "2026-06-11T00:00:00Z",
        "last_event_seq": 0,
        "source_hashes": {"events_count": 0},
    }
    version_control = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "status": "unavailable",
        "provider": "git",
        "repository": {"dirty": False, "dirty_files_count": 0},
        "generated_at": "2026-06-11T00:00:00Z",
        "last_event_seq": 0,
        "source_hashes": {"events_count": 0},
    }
    for filename, payload in {
        "workflow_status.json": workflow_status,
        "plan_index.json": plan_index,
        "workflow_graph.json": workflow_graph,
        "metrics.json": metrics,
        "version_control_status.json": version_control,
    }.items():
        (read_models / filename).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (read_models / "dashboard_feed.jsonl").write_text(
        json.dumps({"schema_version": "1.5", "workflow_id": workflow_id, "event": "canonical_fixture"})
        + "\n",
        encoding="utf-8",
    )
    (read_models / "run_summaries.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.5",
                "workflow_id": workflow_id,
                "run_id": "run_canonical",
                "task_id": "C001",
                "status": "pass",
                "summary": "Canonical run summary.",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def local_port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_local_port_range(width: int) -> int:
    for _attempt in range(200):
        base = free_local_port()
        if base + width - 1 > 65535:
            continue
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + width):
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                sockets.append(sock)
            return base
        except OSError:
            continue
        finally:
            for sock in sockets:
                sock.close()
    raise AssertionError(f"unable to find {width} contiguous free local ports")


def reserve_local_port(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


def update_dashboard_port_config(
    config_path: Path,
    *,
    preferred_port: int,
    port_range: list[int],
    server_state_file: str | None = None,
) -> None:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        config = {"schema_version": "1.5", "enabled": True, "host": "127.0.0.1"}
    config["port"] = "auto"
    config["preferred_port"] = preferred_port
    config["port_range"] = port_range
    if server_state_file is not None:
        config["server_state_file"] = server_state_file
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_json(url: str, token: str, *, body: dict[str, object] | None = None) -> dict[str, object]:
    data = None
    method = "GET"
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def request_text(url: str, token: str) -> str:
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=5) as response:
        return response.read().decode("utf-8")


def request_json_status(
    url: str,
    *,
    token: str | None = None,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = None
    method = "GET"
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def request_status_text(
    url: str,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers.items())
    except HTTPError as error:
        return error.code, error.read().decode("utf-8"), dict(error.headers.items())


def read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
    return records


REQUIRED_RUN_DETAIL_SECTIONS = {
    "prompt",
    "final_output",
    "report",
    "human_summary",
    "logs",
    "validation",
    "artifacts",
    "project_changes",
    "git_checkpoint",
    "diff_summary",
}


def sections_by_key(detail: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(section.get("key")): section
        for section in detail.get("sections", [])
        if isinstance(section, dict)
    }


def path_content_records(value: object, *, prefix: str = "$") -> list[str]:
    records: list[str] = []
    if isinstance(value, dict):
        if (value.get("path") or value.get("markdown_path")) and value.get("content"):
            records.append(prefix)
        for key, child in value.items():
            records.extend(path_content_records(child, prefix=f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            records.extend(path_content_records(child, prefix=f"{prefix}[{index}]"))
    return records


def read_server_startup(process: subprocess.Popen[str]) -> dict[str, object]:
    assert process.stdout is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.1)
        if ready:
            line = process.stdout.readline()
            if not line:
                break
            return json.loads(line)
        if process.poll() is not None:
            break
    stderr = process.stderr.read() if process.stderr is not None else ""
    raise AssertionError(f"dashboard server did not emit startup JSON; exit={process.poll()} stderr={stderr}")


def wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def terminate_pid(pid: object) -> None:
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        os.kill(pid, 15)
    except OSError:
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)


class StaticDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._loopplane_home_tmp = tempfile.TemporaryDirectory()
        self._original_loopplane_home = os.environ.get("LOOPPLANE_HOME")
        os.environ["LOOPPLANE_HOME"] = str(Path(self._loopplane_home_tmp.name) / "loopplane-home")

    def tearDown(self) -> None:
        if self._original_loopplane_home is None:
            os.environ.pop("LOOPPLANE_HOME", None)
        else:
            os.environ["LOOPPLANE_HOME"] = self._original_loopplane_home
        self._loopplane_home_tmp.cleanup()

    def test_markdown_renderer_supports_figures(self) -> None:
        html = _render_markdown_document(
            '![Failure distribution](artifacts/loopplane/results/T001/runs/run_1/artifacts/failure.png "Failure distribution")',
            markdown_path=".loopplane/results/T001/human_summary.md",
        )

        self.assertIn('<figure class="markdown-figure">', html)
        self.assertIn('class="markdown-figure-link"', html)
        self.assertIn('href="artifacts/loopplane/results/T001/runs/run_1/artifacts/failure.png"', html)
        self.assertIn('src="artifacts/loopplane/results/T001/runs/run_1/artifacts/failure.png"', html)
        self.assertIn("<figcaption>Failure distribution</figcaption>", html)

    def test_markdown_renderer_treats_subproject_png_as_project_relative_figure(self) -> None:
        html = _render_markdown_document(
            '![AQI trend](subprojects/air_quality/figures/aqi_trend.png "AQI trend")',
            markdown_path=".loopplane/results/T001/human_summary.md",
        )

        self.assertIn('src="subprojects/air_quality/figures/aqi_trend.png"', html)
        self.assertNotIn(".loopplane/results/T001/subprojects", html)

    def test_markdown_renderer_treats_deliverable_png_as_project_relative_figure(self) -> None:
        html = _render_markdown_document(
            '![Revenue](../../../sales_analysis/revenue_trend.png "Revenue trend")\n\n- ![Inline](sales_analysis/category_breakdown.png "Category chart")',
            markdown_path=".loopplane/results/P2.T001/human_summary.md",
        )

        self.assertIn('src="sales_analysis/revenue_trend.png"', html)
        self.assertIn('class="markdown-inline-figure"', html)
        self.assertIn('src="sales_analysis/category_breakdown.png"', html)
        self.assertNotIn(".loopplane/sales_analysis", html)

    def test_markdown_renderer_links_explicit_links_without_auto_linking_plain_slashes(self) -> None:
        html = _render_markdown_document(
            "Open [plot](artifacts/plot.png), compare Pythia/GPT-NeoX, note 2/4 wins, inspect sales_analysis/REPORT.md, inspect subprojects/air_quality/report.md, or inspect .loopplane/results/T001/runs/run_1/artifacts/table.csv.",
            markdown_path=".loopplane/results/T001/human_summary.md",
        )

        self.assertIn('<a class="markdown-link" href="artifacts/plot.png"', html)
        self.assertIn(">plot</a>", html)
        self.assertIn("Pythia/GPT-NeoX", html)
        self.assertIn("2/4 wins", html)
        self.assertIn("sales_analysis/REPORT.md", html)
        self.assertIn("subprojects/air_quality/report.md", html)
        self.assertIn(".loopplane/results/T001/runs/run_1/artifacts/table.csv", html)
        self.assertNotIn("markdown-plain-link", html)
        self.assertNotIn("Pythia/GPT-NeoX</a>", html)
        self.assertNotIn("2/4</a>", html)
        self.assertNotIn("sales_analysis/REPORT.md</a>", html)

    def test_markdown_renderer_rewrites_absolute_project_links(self) -> None:
        html = _render_markdown_document(
            "- [Report](/tmp/loopplane-project/sales_analysis/REPORT.md)",
            markdown_path=".loopplane/results/T001/final.md",
            project_root="/tmp/loopplane-project",
        )

        self.assertIn('href="sales_analysis/REPORT.md"', html)
        self.assertIn(">Report</a>", html)
        self.assertNotIn("/tmp/loopplane-project", html)
        self.assertNotIn("noneReportnone", html)

    def test_markdown_renderer_unescapes_url_entities_once(self) -> None:
        html = _render_markdown_document(
            "evidence: [.loopplane/results/P1.T001/](http://127.0.0.1:3766/api/workflows/wf/files?token=t&amp;path=.loopplane%2Fresults%2FP1.T001)",
            markdown_path=".loopplane/results/P1.T001/human_summary.md",
        )

        self.assertIn("token=t&amp;path=", html)
        self.assertNotIn("amp;amp", html)
        self.assertNotIn("nonenone", html)

    def test_markdown_renderer_wraps_tables_and_fenced_code_cleanly(self) -> None:
        html = _render_markdown_document(
            "| Field | Value |\n| --- | --- |\n| Status | Done |\n\n```text\nok\n```",
            markdown_path=".loopplane/results/T001/human_summary.md",
        )

        self.assertIn('<div class="markdown-table-wrap"><table>', html)
        self.assertIn("<td>Done</td>", html)
        self.assertIn('<pre class="markdown-code"><code>ok</code></pre>', html)
        css = DASHBOARD_CSS.read_text(encoding="utf-8")
        self.assertIn(".file-preview-content {\n  border-radius: 0;\n  max-block-size: 100%;\n  max-height: none;\n  min-height: 0;\n  overflow: auto;", css)
        self.assertIn(".markdown-document {\n  border: 1px solid var(--line);\n  background: var(--surface-muted);\n  display: block;", css)
        self.assertIn(".markdown-document > * + * {\n  margin-top: 8px;\n}", css)
        self.assertIn(".markdown-preview-content {\n  margin-top: 0;\n  max-height: none;\n  overflow-x: hidden;\n  overflow-y: auto;", css)
        self.assertIn(".markdown-preview-content .markdown-code {\n  max-height: none;", css)
        self.assertIn(
            ".markdown-preview-content .markdown-table-wrap {\n  max-height: none;\n  overflow-x: auto;\n  overflow-y: hidden;",
            css,
        )
        self.assertIn("@supports (overflow: clip)", css)

    def test_objective_status_pill_uses_short_verification_label(self) -> None:
        html = _status_pill("needs_verification")

        self.assertIn(">Verify</span>", html)
        self.assertNotIn("Needs Verification", html)

    def test_expanded_work_uses_source_note_in_static_and_live_renderers(self) -> None:
        static_js = DASHBOARD_JS.read_text(encoding="utf-8")

        self.assertIn("expansion-note", static_js)
        self.assertIn(">Self-expansion</small>", static_js)
        self.assertNotIn("expansion-marker", static_js)
        self.assertNotIn('">+</span>', static_js)

    def test_static_dashboard_consumes_read_models_without_authoritative_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, run_dir = prepare_dashboard_project(project)
            authoritative_paths = [
                project / "PLAN.md",
                paths.runtime_dir / "state.json",
                paths.runtime_dir / "events" / "events_000001.jsonl",
                paths.results_dir / "T001" / "latest.json",
                run_dir / "validation.json",
            ]
            before = file_hashes(project, authoritative_paths)

            result = render_static_dashboard(project)

            after = file_hashes(project, authoritative_paths)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(after, before)
            self.assertEqual(result["status"], "rendered")
            self.assertEqual(
                {
                    "workflow_status",
                    "plan_checklist",
                    "workflow_graph",
                    "node_details",
                    "activity_feed",
                    "git_checkpoint_status",
                    "runner_configuration",
                    "planning_controls",
                    "read_model_freshness",
                    "read_model_rebuild_request",
                    "control_requests",
                    "approval_panel",
                    "inspector_console",
                    "change_requests",
                    "requires_attention",
                    "workspace_selector",
                },
                set(result["covered_sections"]),
            )

            output_dir = project / result["dashboard_dir"]
            index_file = project / result["index_file"]
            self.assertTrue(index_file.is_file())
            for filename in ("loopplane_logo.png", "loopplane_logo_dark.png", "loopplane_logo_light.png"):
                self.assertIn(f"{result['dashboard_dir'].rstrip('/')}/{filename}", result["generated_files"])
                self.assertTrue((output_dir / filename).is_file())
            for filename in READ_MODEL_FILES:
                self.assertTrue((output_dir / "read_models" / filename).is_file(), filename)

            html = index_file.read_text(encoding="utf-8")
            for text in (
                "Workflow Status",
                "Elapsed",
                "Plan Checklist",
                "plan-view-toggle",
                'data-plan-view="checklist"',
                'data-plan-view-panel="markdown"',
                "Full PLAN.md",
                "markdown-document",
                "phase-timing",
                "phase-duration-bar",
                "--phase-duration-pct: 100%",
                "phase-progress",
                "--phase-progress-pct: 100%",
                "Workflow Graph",
                "graph-overview",
                "graph-phase-group",
                "graph-task-rail",
                "Graph Mode",
                "Agent Pipeline",
                "graph-expand-toggle",
                "graph-pipeline-scroll",
                'data-graph-mode="phase_pipeline"',
                'data-collapsible="true"',
                "panel-collapse-toggle",
                'data-panel-key="feed"',
                'data-default-collapsed="false"',
                'id="activity-feed-body" class="panel-body">',
                "Node Details",
                "Activity Feed",
                "Git Checkpoint Status",
                "Produce result artifact",
                "human-summary-trigger",
                'data-detail-render="markdown"',
                "Strategic Progress Reading",
                "clearer project asset",
                "Task T001 validation passed.",
                "dashboard_smoke_tail",
                "version_control_status.json",
                'data-read-model-file="workflow_status.json"',
                "detail-file-button",
                "file-preview-modal",
                "dashboard-loading-overlay",
                "dashboard-loading-spinner",
                "loopplane-read-models",
                "Freshness",
                "Inspector Chat / Change Request Console",
                "inspector-chat-form",
                "change-request-form",
                "Ask Inspector",
                "Create Change Request",
                "Full Agent Inspector",
                "Context Paths",
                "Change Requests",
                "Planning Controls",
                "Run Planner",
                "Run Auditor",
                "Activate Plan",
                "Execution Requests",
                "execution-control-form",
                "Start",
                "Pause",
                "Resume",
                "Stop",
                "Static dashboard is read-only",
                'class="app-header"',
                "Static Dashboard",
                "dashboard-refresh-button",
                "dashboard-last-refreshed",
                "Static snapshot; live refresh unavailable.",
                'data-theme="dark"',
                'id="theme-toggle"',
                "Validation Fixture Workflow",
            ):
                self.assertIn(text, html)
            self.assertIn('data-status-tier="success"', html)
            self.assertRegex(html, r'<button id="dashboard-refresh-button" type="button" disabled>')
            static_js = (output_dir / "static_dashboard.js").read_text(encoding="utf-8")
            static_css = (output_dir / "static_dashboard.css").read_text(encoding="utf-8")
            self.assertIn("data-log-stream-title", static_js)
            self.assertIn("&tail=1&max_lines=", static_js)
            self.assertIn("Follow log tail", static_js)
            self.assertIn("scrollPreviewToBottom", static_js)
            self.assertIn("logStreamInFlight", static_js)
            self.assertIn("scrollToBottom: true", static_js)
            self.assertIn("renderMarkdownTable", static_js)
            self.assertIn("markdown-table-wrap", static_js)
            self.assertIn("renderMarkdownFigure", static_js)
            self.assertIn("markdownAssetUrl", static_js)
            self.assertNotIn("markdown-plain-link", static_js)
            self.assertLess(
                static_js.index('\'<ol class="task-list">\' + taskRows'),
                static_js.index('renderObjectiveList(objectiveRows, "Phase objectives")'),
            )
            self.assertLess(
                static_js.index("checklistBlocks.join(\"\")"),
                static_js.index('renderObjectiveList(workflowObjectives, "Workflow objectives")'),
            )
            self.assertIn("dashboard-loading-spin", static_css)
            self.assertIn(".app-header", static_css)
            self.assertIn(".app-logo", static_css)
            self.assertIn(".markdown-table-wrap", static_css)
            self.assertIn("overflow-y: hidden", static_css)
            self.assertIn("overflow-y: clip", static_css)
            self.assertIn(".markdown-document table", static_css)
            self.assertIn(".markdown-code code", static_css)
            self.assertIn(".markdown-figure img", static_css)
            self.assertIn(".markdown-link", static_css)
            self.assertNotIn(".markdown-plain-link", static_css)
            self.assertIn("grid-template-rows: auto minmax(0, 1fr)", static_css)
            self.assertIn(".markdown-preview-content .markdown-code", static_css)

            script_document = dashboard_script_document(html)
            self.assertEqual(script_document.get("payload_encoding"), "gzip+base64")
            self.assertLess(script_document["payload_compressed_bytes"], script_document["payload_uncompressed_bytes"])

            payload = dashboard_script_payload(html)
            self.assertEqual(payload["workflow_title"], "Validation Fixture Workflow")
            self.assertIn("static_project_root_href", payload)
            self.assertEqual(payload["read_models"]["plan_index.json"]["workflow_title"], "Validation Fixture Workflow")
            self.assertEqual(payload["read_models"]["workflow_status.json"]["progress"]["completed_tasks"], 1)
            self.assertTrue(payload["read_models"]["workflow_graph.json"]["nodes"])
            validation_nodes = [
                node
                for node in payload["read_models"]["workflow_graph.json"]["nodes"]
                if node.get("type") == "validation"
            ]
            self.assertTrue(validation_nodes)
            self.assertTrue(all(node.get("started_at") and node.get("ended_at") for node in validation_nodes))
            self.assertTrue(all(isinstance(node.get("elapsed_seconds"), int) for node in validation_nodes))
            plan_index = payload["read_models"]["plan_index.json"]
            self.assertEqual(plan_index["tasks"][0]["human_summary"]["status"], "ready")
            self.assertEqual(plan_index["phases"][0]["human_summary"]["status"], "ready")
            self.assertIn("Strategic Progress Reading", plan_index["tasks"][0]["human_summary"]["content"])
            self.assertIn("clearer project asset", plan_index["tasks"][0]["human_summary"]["content"])
            self.assertEqual(payload["plan_markdown"]["path"], "PLAN.md")
            self.assertIn("Phase P0: Validation Fixture", payload["plan_markdown"]["content"])
            self.assertIn("node_details", payload)
            run_summaries = payload["jsonl_models"]["run_summaries.jsonl"]
            self.assertTrue(run_summaries)
            self.assertTrue(all("details" not in record for record in run_summaries))
            run_detail = payload["node_details"]["runs"]["run_fixture"]
            self.assertEqual(run_detail["source"], "run_details")
            run_sections = sections_by_key(run_detail)
            self.assertTrue(REQUIRED_RUN_DETAIL_SECTIONS.issubset(run_sections), sorted(run_sections))
            for section_key in REQUIRED_RUN_DETAIL_SECTIONS:
                self.assertTrue(run_sections[section_key]["available"], section_key)
            self.assertIn("Worker Prompt", run_sections["prompt"]["content"])
            self.assertEqual(run_sections["final_output"]["title"], "Final Response")
            self.assertIn("Worker Final Output", run_sections["final_output"]["content"])
            self.assertIn("Worker claims completion", run_sections["report"]["content"])
            self.assertIn("Strategic Progress Reading", run_sections["human_summary"]["content"])
            self.assertEqual(run_sections["prompt"]["render_mode"], "markdown")
            self.assertEqual(run_sections["final_output"]["render_mode"], "markdown")
            self.assertEqual(run_sections["report"]["render_mode"], "markdown")
            self.assertEqual(run_sections["human_summary"]["render_mode"], "markdown")
            self.assertTrue(str(run_sections["human_summary"]["path"]).endswith("human_summary.md"))
            self.assertIn("Task T001 validation passed", run_sections["validation"]["content"])
            self.assertEqual(run_sections["diff_summary"]["changed_files_count"], 1)
            self.assertEqual(run_sections["diff_summary"]["changed_files"][0]["path"], "src/app.py")
            figure_artifacts = [
                item
                for item in run_sections["artifacts"]["items"]
                if str(item.get("path", "")).endswith("scorecard.svg")
            ]
            self.assertEqual(figure_artifacts[0]["render_mode"], "image")
            self.assertEqual(run_sections["git_checkpoint"]["before"]["checkpoint_id"], "cp_before_run_fixture")
            self.assertEqual(run_sections["git_checkpoint"]["after"]["checkpoint_id"], "cp_after_run_fixture")
            self.assertNotIn("events", run_sections)
            event_nodes = [
                node
                for node in payload["read_models"]["workflow_graph.json"]["nodes"]
                if node.get("type") == "event"
            ]
            self.assertTrue(event_nodes)
            event_detail = payload["node_details"]["nodes"][event_nodes[0]["node_id"]]
            event_sections = sections_by_key(event_detail)
            self.assertEqual(event_detail["source"], "workflow_graph.json")
            self.assertIn("event_details", event_sections)
            self.assertNotIn("events", event_sections)
            run_detail_json = json.dumps(run_detail, sort_keys=True)
            self.assertIn("[REDACTED]", run_detail_json)
            self.assertNotIn("super-secret-token", run_detail_json)
            self.assertNotIn("abcdefghijklmnop", run_detail_json)
            self.assertNotIn(".git/config", run_detail_json)
            self.assertNotIn("refs/loopplane/checkpoints", run_detail_json)
            self.assertNotIn("repository_root", run_detail_json)
            self.assertEqual(payload["read_model_freshness"]["status"], "current")
            self.assertIn("read_model_rebuild", payload)
            self.assertEqual(payload["read_model_rebuild"]["endpoint"], f"/api/workflows/{payload['workflow_id']}/rebuild-read-models")
            self.assertTrue(payload["read_model_rebuild"]["mutation_allowed"])
            self.assertIn("planning_controls", payload)
            self.assertFalse(payload["server_mode"])
            self.assertTrue(payload["planning_controls"]["mutation_allowed"])
            self.assertEqual(payload["planning_controls"]["endpoints"]["plan"], f"/api/workflows/{payload['workflow_id']}/plan")
            self.assertIn("execution_controls", payload)
            self.assertTrue(payload["execution_controls"]["mutation_allowed"])
            self.assertEqual(
                payload["execution_controls"]["endpoints"],
                {
                    "start": f"/api/workflows/{payload['workflow_id']}/control-requests",
                    "pause": f"/api/workflows/{payload['workflow_id']}/control-requests",
                    "resume": f"/api/workflows/{payload['workflow_id']}/control-requests",
                    "stop": f"/api/workflows/{payload['workflow_id']}/control-requests",
                },
            )
            self.assertIn("inspector_console", payload)
            self.assertEqual(
                payload["inspector_console"]["endpoints"]["chat"],
                f"/api/workflows/{payload['workflow_id']}/chat",
            )
            self.assertEqual(
                payload["inspector_console"]["endpoints"]["change_request"],
                f"/api/workflows/{payload['workflow_id']}/change-requests",
            )
            self.assertTrue(payload["inspector_console"]["chat_allowed"])
            self.assertTrue(payload["inspector_console"]["change_request_allowed"])
            self.assertIn("PLAN.md", payload["inspector_console"]["allowed_paths"])
            self.assertIn("disabled", re.search(r'<form id="execution-control-form".+?</form>', html, re.S).group(0))
            self.assertIn("disabled", re.search(r'<form id="inspector-chat-form".+?</form>', html, re.S).group(0))
            self.assertIn("disabled", re.search(r'<form id="change-request-form".+?</form>', html, re.S).group(0))

    def test_static_dashboard_renders_objective_checklists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            plan_path = project / "PLAN.md"
            plan_path.write_text(
                plan_path.read_text(encoding="utf-8")
                + """

### Phase Objective Checklist

- [ ] `PO1` Dashboard exposes objective closure.
  - evidence_scope: .loopplane/results/T001/
  - judgment_guidance: Confirm the dashboard exposes objective state.
  - verifier: objective_verifier
  - unmet_action: self_expand

## Final Objective Checklist

- [ ] `FO1` Workflow objective appears in dashboard.
  - evidence_scope: .loopplane/results/
  - judgment_guidance: Confirm workflow-level objective visibility.
  - verifier: objective_verifier
  - unmet_action: self_expand
""",
                encoding="utf-8",
            )
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))

            result = render_static_dashboard(project, output_dir=project / "dashboard_objectives")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("objective-section", html)
            self.assertIn("Phase objectives", html)
            self.assertIn("Workflow objectives", html)
            self.assertIn("PO1", html)
            self.assertIn("FO1", html)
            self.assertLess(html.index('<ol class="task-list">'), html.index("Phase objectives"))
            self.assertLess(html.index("Phase objectives"), html.index("Workflow objectives"))

    def test_dashboard_marks_expanded_work_and_running_graph_times(self) -> None:
        plan_index = {
            "objectives": [],
            "phases": [
                {
                    "phase_id": "P1",
                    "title": "Phase P1: Expanded Follow-up",
                    "status": "in_progress",
                    "expanded": True,
                    "expansion_marker": "+",
                    "expansion": {"proposal_id": "exp_fixture", "source": "self_expansion"},
                    "tasks": [
                        {
                            "task_id": "EXP001",
                            "title": "Expanded evidence task",
                            "status": "pending",
                            "validation_status": "pending",
                            "expanded": True,
                            "expansion_marker": "+",
                            "expansion": {"proposal_id": "exp_fixture", "source": "self_expansion"},
                        }
                    ],
                }
            ],
        }
        workflow_graph = {
            "source_hashes": {"events_count": 300},
            "event_window": {"total_events": 300, "visible_event_nodes": 2, "limit": 2, "truncated": True},
            "self_expansion_aggregation": {
                "enabled": True,
                "visible_node_count": 3,
                "aggregated_node_count": 42,
                "detail_limit": 50,
                "groups": [],
            },
            "nodes": [
                {
                    "node_id": "run_EXP001",
                    "type": "worker",
                    "status": "running",
                    "active": True,
                    "task_id": "EXP001",
                    "run_id": "run_EXP001",
                    "title": "Expanded evidence task",
                    "started_at": "2026-06-18T10:00:00Z",
                    "ended_at": "2026-06-18T10:01:00Z",
                },
                {
                    "node_id": "event_299",
                    "type": "event",
                    "status": "scheduler_tick",
                    "title": "Scheduler tick",
                    "started_at": "2026-06-18T10:02:00Z",
                    "ended_at": "2026-06-18T10:02:00Z",
                    "event_sequence": 299,
                },
                {
                    "node_id": "event_300",
                    "type": "event",
                    "status": "scheduler_waiting",
                    "title": "Scheduler waiting",
                    "started_at": "2026-06-18T10:03:00Z",
                    "ended_at": "2026-06-18T10:03:00Z",
                    "event_sequence": 300,
                },
            ],
            "edges": [],
        }

        plan_html = _render_plan_panel(
            plan_index,
            {"content": "", "path": "PLAN.md"},
            workflow_graph=workflow_graph,
            payload={"rendered_at": "2026-06-18T10:05:00Z"},
        )
        graph_html = _render_graph_panel(workflow_graph, plan_index)

        combined = plan_html + graph_html
        self.assertIn('class="expansion-note"', combined)
        self.assertIn(">Self-expansion</small>", combined)
        self.assertNotIn('class="expansion-marker"', combined)
        self.assertNotIn('">+</span>', combined)
        self.assertIn('title="Phase added by self-expansion"', combined)
        self.assertIn('title="Task added by self-expansion"', combined)
        self.assertIn('data-expanded="true"', combined)
        self.assertIn("End</small><strong>Present</strong>", plan_html)
        self.assertIn("06-18 10:00 -&gt; Present", graph_html)
        self.assertNotIn("06-18 10:00 -&gt; 06-18 10:01", graph_html)
        self.assertIn("<strong>2</strong> recent events<small>of 300</small>", graph_html)
        self.assertIn("<strong>42</strong> self-expansion aggregated", graph_html)

    def test_static_dashboard_shows_ready_planning_draft_before_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Dashboard ready draft.")
            workflow = load_workflow_config(project)
            paths = WorkflowPaths.from_config(project, workflow)
            draft_path = write_ready_plan_draft(project, paths, str(workflow["workflow_id"]))

            result = render_static_dashboard(
                project,
                output_dir=project / "dashboard_ready_draft",
                rebuild_read_models_first=True,
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            payload = dashboard_script_payload((project / result["index_file"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["plan_markdown"]["path"], draft_path.relative_to(project).as_posix())
            self.assertEqual(payload["plan_markdown"]["active_plan_file"], paths.value("plan_file"))
            self.assertEqual(payload["plan_markdown"]["plan_source"]["kind"], "planning_draft")
            self.assertIn("Phase P0: Draft Checklist", payload["plan_markdown"]["content"])
            plan_index = payload["read_models"]["plan_index.json"]
            self.assertEqual(plan_index["summary"]["total"], 1)
            self.assertEqual(plan_index["tasks"][0]["task_id"], "T001")
            self.assertEqual(plan_index["plan_source"]["kind"], "planning_draft")

    def test_static_dashboard_graph_overflow_nodes_are_expandable_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            graph_path = paths.read_models_dir / "workflow_graph.json"
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            template_node = dict(graph["nodes"][0])
            graph["nodes"] = [
                {
                    **template_node,
                    "node_id": "node_runtime_extra_agent",
                    "run_id": "run_runtime_extra_agent",
                    "task_id": "T001",
                    "type": "worker",
                    "status": "running",
                    "title": "Runtime extra agent",
                }
            ] + [
                {
                    **template_node,
                    "node_id": f"node_runtime_extra_event_{index}",
                    "run_id": "run_runtime_extra_agent",
                    "task_id": "T001",
                    "type": "event",
                    "status": f"event_{index}",
                    "title": f"Runtime event {index}",
                    "created_at": f"2026-06-11T00:10:{index:02d}Z",
                    "summary": {"one_line": f"Runtime event {index} was recorded.", "highlights": [], "risks": []},
                }
                for index in range(8)
            ]
            graph_path.write_text(json.dumps(graph, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = render_static_dashboard(project, output_dir=project / "dashboard_graph_overflow")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Agent Pipeline", html)
            self.assertIn("Expand All", html)
            self.assertIn("Lifecycle", html)
            self.assertIn('class="graph-node-more"', html)
            self.assertIn("Show 2 more related records", html)
            self.assertIn('data-overflow-node="true" hidden', html)
            self.assertIn('data-node-id="node_runtime_extra_event_7"', html)
            self.assertLess(
                html.index('data-node-id="node_runtime_extra_event_7"'),
                html.index('data-node-id="node_runtime_extra_event_6"'),
            )
            self.assertLess(
                html.index('data-node-id="node_runtime_extra_event_2"'),
                html.index('data-node-id="node_runtime_extra_event_1"'),
            )
            self.assertIn('data-status="running" data-status-tier="active"', html)

    def test_static_dashboard_marks_active_lease_changes_as_stale_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow = load_workflow_config(project)
            lease_dir = paths.runtime_dir / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            (lease_dir / "run_live.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": workflow["workflow_id"],
                        "run_id": "run_live",
                        "node_id": "node_worker_T001_run_live",
                        "task_id": "T001",
                        "role": "worker",
                        "runner_id": "worker",
                        "status": "running",
                        "prepared_at": "2026-06-11T00:10:00Z",
                        "heartbeat_at": "2026-06-11T00:10:05Z",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = render_static_dashboard(project, output_dir=project / "dashboard_active_lease_stale")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            payload = dashboard_script_payload(html)
            self.assertEqual(payload["read_model_freshness"]["status"], "stale")
            warnings = payload["read_model_freshness"]["warnings"]
            self.assertTrue(any(warning["code"] == "read_model_active_leases_stale" for warning in warnings))
            self.assertIn("active-run lease state", html)

    def test_static_dashboard_materializes_v15_flat_workflow_registry_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, run_dir = prepare_dashboard_project(project)
            workflow_id = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                "workflow_id"
            ]
            preserved_paths = [
                project / "PROJECT_BRIEF.md",
                project / "PLAN.md",
                project / ".loopplane" / "SHARED_CONTEXT.md",
                project / ".loopplane" / "config" / "workflow.json",
                paths.runtime_dir / "state.json",
                paths.runtime_dir / "events" / "events_000001.jsonl",
                paths.read_models_dir / "workflow_status.json",
                paths.read_models_dir / "plan_index.json",
                paths.read_models_dir / "workflow_graph.json",
                paths.read_models_dir / "dashboard_feed.jsonl",
                paths.read_models_dir / "run_summaries.jsonl",
                paths.read_models_dir / "metrics.json",
                paths.read_models_dir / "version_control_status.json",
                paths.results_dir / "T001" / "latest.json",
                run_dir / "validation.json",
            ]
            before = file_hashes(project, preserved_paths)
            for relative in ("workspace.json", "workflow_registry.json", "current_workflow.json"):
                (project / ".loopplane" / relative).unlink()

            result = render_static_dashboard(project, output_dir=project / "dashboard_flat_compat")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(file_hashes(project, preserved_paths), before)
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            current = json.loads((project / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8"))
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            self.assertEqual(len(registry["workflows"]), 1)
            record = registry["workflows"][0]
            self.assertEqual(record["workflow_id"], workflow_id)
            self.assertEqual(record["workflow_root"], ".loopplane/")
            self.assertEqual(record["plan_file"], "PLAN.md")
            self.assertEqual(record["read_models_dir"], ".loopplane/read_models")
            self.assertEqual(current["current_workflow_id"], workflow_id)
            self.assertEqual(workspace["current_workflow_id"], workflow_id)
            self.assertEqual(result["workflow_id"], workflow_id)

            index_file = project / result["index_file"]
            html = index_file.read_text(encoding="utf-8")
            self.assertIn("v1.5 compatibility-flat workflow", html)
            self.assertIn(workflow_id, html)

    def test_static_dashboard_dom_smoke_executes_javascript_surfaces(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required for the static dashboard DOM smoke")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            figure_path = project / "sales_analysis" / "revenue_trend.png"
            figure_path.parent.mkdir(parents=True, exist_ok=True)
            figure_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture-png")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            enable_approvals(project)
            append_pending_approval(
                project,
                workflow_id,
                "approval_static_browser_fixture",
                message="Approve static browser fixture.",
            )
            change = submit_change_request(
                project,
                "Browser smoke change request.",
                source="dashboard_static_browser_smoke",
            )
            self.assertTrue(change["ok"], json.dumps(change, indent=2, sort_keys=True))
            archived_workflow_id = "wf_static_browser_archived"
            write_canonical_read_models(project, archived_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_static_browser_smoke",
                "workflows": [
                    {
                        "workflow_id": workflow_id,
                        "name": "browser static current",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": archived_workflow_id,
                        "name": "browser static archived",
                        "status": "archived_view",
                        "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                        "read_only": True,
                        "archived": True,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_static_browser_smoke",
                        "current_workflow_id": workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()

            result = render_static_dashboard(
                project,
                output_dir=project / "dashboard_browser_static",
                embed_workflow_snapshots=True,
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "rendered_with_warnings")
            self.assertEqual(result["read_model_freshness"]["status"], "stale")
            smoke = subprocess.run(
                [
                    node,
                    str(REPO_ROOT / "tests" / "dashboard_dom_smoke.js"),
                    str(project / result["index_file"]),
                    str(project / result["dashboard_dir"] / "static_dashboard.js"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                smoke.returncode,
                0,
                f"stdout:\n{smoke.stdout}\nstderr:\n{smoke.stderr}",
            )
            summary = json.loads(smoke.stdout)
            self.assertTrue(summary["ok"], json.dumps(summary, indent=2, sort_keys=True))
            self.assertEqual(summary["workflow_id"], workflow_id)
            self.assertEqual(summary["archived_workflow_id"], archived_workflow_id)
            self.assertIn("node details prompt", summary["checks"])
            self.assertIn("stale freshness", summary["checks"])
            self.assertIn("archived control disabled", summary["checks"])
            self.assertEqual(current_path.read_bytes(), current_before)

    def test_static_dashboard_warns_when_read_models_are_stale_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            stale_event = append_event(
                paths,
                workflow_id=workflow_config["workflow_id"],
                event_type="post_rebuild_stale_marker",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )
            authoritative_paths = [
                project / "PLAN.md",
                paths.runtime_dir / "state.json",
                paths.runtime_dir / "events" / "events_000001.jsonl",
                paths.results_dir / "T001" / "latest.json",
                run_dir / "validation.json",
            ]
            before = file_hashes(project, authoritative_paths)

            result = render_static_dashboard(project, output_dir=project / "dashboard_stale")

            after = file_hashes(project, authoritative_paths)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(after, before)
            self.assertEqual(result["status"], "rendered_with_warnings")
            self.assertTrue(result["warnings"])
            freshness = result["read_model_freshness"]
            self.assertEqual(freshness["status"], "stale")
            self.assertEqual(freshness["event_log"]["source_event_id"], stale_event["event_id"])
            self.assertEqual(freshness["event_log"]["last_event_seq"], stale_event["sequence"])
            self.assertIn("loopplane rebuild-read-models --project", freshness["rebuild_command"])
            self.assertTrue(any(warning["code"] == "read_model_stale" for warning in freshness["warnings"]))

            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Read Models May Be Stale", html)
            self.assertIn("Rebuild command", html)
            self.assertIn("loopplane rebuild-read-models --project", html)
            self.assertIn("read-model-rebuild-form", html)
            self.assertIn("Create Rebuild Request", html)
            self.assertIn("Static dashboard is read-only; open server mode to create a rebuild request record.", html)
            self.assertIn("disabled", re.search(r'<form id="read-model-rebuild-form".+?</form>', html, re.S).group(0))

            payload = dashboard_script_payload(html)
            self.assertEqual(payload["read_model_freshness"]["status"], "stale")
            self.assertEqual(payload["read_model_freshness"]["event_log"]["source_event_id"], stale_event["event_id"])
            self.assertEqual(payload["read_model_rebuild"]["control_type"], "rebuild_read_models")
            self.assertEqual(payload["read_model_rebuild"]["endpoint"], f"/api/workflows/{payload['workflow_id']}/rebuild-read-models")
            self.assertEqual(payload["read_model_rebuild"]["pending_count"], 0)
            self.assertFalse(payload["read_model_rebuild"]["rebuild_in_progress"])
            self.assertTrue(payload["read_model_rebuild"]["request_allowed"])

    def test_static_dashboard_shows_wait_state_for_pending_read_model_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            stale_event = append_event(
                paths,
                workflow_id=workflow_config["workflow_id"],
                event_type="post_rebuild_pending_marker",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )
            request = record_control_request(
                project,
                "rebuild_read_models",
                source="test",
                payload={"reason": "dashboard opened while rebuild is pending"},
            )
            self.assertTrue(request["ok"], json.dumps(request, indent=2, sort_keys=True))

            result = render_static_dashboard(project, output_dir=project / "dashboard_pending_rebuild")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["read_model_freshness"]["status"], "stale")
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Rebuild In Progress", html)
            self.assertIn("Read Models Are Rebuilding", html)
            self.assertIn("A read-model rebuild request is already pending.", html)
            self.assertIn("Rebuild Request Pending", html)
            self.assertNotIn("Create Rebuild Request", re.search(r'<form id="read-model-rebuild-form".+?</form>', html, re.S).group(0))
            self.assertIn('data-rebuild-action="rebuild_read_models" disabled', html)

            payload = dashboard_script_payload(html)
            self.assertEqual(payload["read_model_freshness"]["event_log"]["source_event_id"], stale_event["event_id"])
            self.assertEqual(payload["read_model_rebuild"]["status"], "rebuilding")
            self.assertTrue(payload["read_model_rebuild"]["rebuild_in_progress"])
            self.assertFalse(payload["read_model_rebuild"]["request_allowed"])
            self.assertEqual(payload["read_model_rebuild"]["pending_count"], 1)
            self.assertEqual(payload["read_model_rebuild"]["recent"][-1]["type"], "rebuild_read_models")

    def test_static_dashboard_suppresses_warning_for_scheduler_waiting_live_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            workflow_id = workflow_config["workflow_id"]
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_started",
                data={"owner": "test", "max_ticks": 1},
                snapshot_interval=None,
            )
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_tick",
                data={"owner": "test", "tick_index": 1},
                snapshot_interval=None,
            )
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_action_selected",
                data={
                    "owner": "test",
                    "action": "wait_background_job",
                    "selected": {},
                    "would_wait": True,
                    "reason": "Background job is still running.",
                },
                snapshot_interval=None,
            )
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_waiting",
                data={"owner": "test", "status": "waiting_background_job", "reason": "Background job is still running."},
                snapshot_interval=None,
            )
            append_event(
                paths,
                workflow_id=workflow_id,
                event_type="scheduler_exited",
                data={
                    "owner": "test",
                    "ticks": 1,
                    "ticks_run": 1,
                    "last_action": "wait_background_job",
                    "stopped_reason": "waiting_background_job",
                    "pending_tasks": 0,
                },
                snapshot_interval=None,
            )

            result = render_static_dashboard(project, output_dir=project / "dashboard_live_drift")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "rendered")
            self.assertEqual(result["warnings"], [])
            freshness = result["read_model_freshness"]
            self.assertEqual(freshness["status"], "current_with_live_drift")
            self.assertEqual(freshness["warnings"], [])
            self.assertTrue(freshness["suppressed_warnings"])
            self.assertEqual(freshness["live_drift"]["event_count"], 5)
            self.assertEqual(freshness["live_drift"]["event_types"]["scheduler_waiting"], 1)

            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertNotIn("Freshness Warning", html)
            self.assertNotIn("Read Models May Be Stale", html)
            payload = dashboard_script_payload(html)
            self.assertEqual(payload["read_model_freshness"]["status"], "current_with_live_drift")

    def test_static_dashboard_warns_when_freshness_metadata_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            status_path = paths.read_models_dir / "workflow_status.json"
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            del status_payload["generated_at"]
            del status_payload["source_hashes"]
            status_path.write_text(json.dumps(status_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = render_static_dashboard(project, output_dir=project / "dashboard_missing_freshness_metadata")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["read_model_freshness"]["status"], "unknown")
            self.assertTrue(
                any(warning["code"] == "read_model_metadata_missing" for warning in result["read_model_freshness"]["warnings"])
            )
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Read Model Freshness Needs Rebuild", html)
            self.assertIn("workflow_status.json is missing freshness metadata.", html)
            self.assertIn("Create Rebuild Request", html)

    def test_dashboard_rebuild_option_refreshes_stale_read_models_before_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            stale_event = append_event(
                paths,
                workflow_id=workflow_config["workflow_id"],
                event_type="post_rebuild_refresh_marker",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )

            result = render_static_dashboard(
                project,
                output_dir=project / "dashboard_refreshed",
                rebuild_read_models_first=True,
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "rendered")
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["read_model_freshness"]["status"], "current")
            workflow_status = result["read_model_freshness"]["read_model"]
            self.assertEqual(workflow_status["source_event_id"], stale_event["event_id"])
            self.assertEqual(workflow_status["last_event_seq"], stale_event["sequence"])

    def test_static_dashboard_renders_workspace_selector_with_workflow_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            archived_workflow_id = "wf_static_selector_archived"
            missing_workflow_id = "wf_static_selector_missing"
            write_canonical_read_models(project, archived_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_static_selector",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "current flat workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                        "summary": {
                            "one_line": "Current workflow selector fixture.",
                            "tasks_total": 1,
                            "tasks_completed": 1,
                            "tasks_blocked": 0,
                        },
                    },
                    {
                        "workflow_id": archived_workflow_id,
                        "name": "archived canonical workflow",
                        "status": "archived_view",
                        "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                        "read_only": True,
                        "archived": True,
                        "summary": {
                            "one_line": "Archived selector fixture.",
                            "tasks_total": 1,
                            "tasks_completed": 0,
                            "tasks_blocked": 0,
                        },
                    },
                    {
                        "workflow_id": missing_workflow_id,
                        "name": "missing read model workflow",
                        "status": "unknown",
                        "workflow_root": f".loopplane/workflows/{missing_workflow_id}",
                        "read_only": False,
                        "archived": False,
                    },
                ],
            }
            registry_path = project / ".loopplane" / "workflow_registry.json"
            current_path = project / ".loopplane" / "current_workflow.json"
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_static_selector",
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()

            result = render_static_dashboard(
                project,
                output_dir=project / "dashboard_selector",
                embed_workflow_snapshots=True,
            )

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("workspace_selector", result["covered_sections"])
            self.assertTrue(result["embed_workflow_snapshots"])
            self.assertEqual(current_path.read_bytes(), current_before)
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Workflow History Selector", html)
            self.assertIn('id="workflow-selector"', html)
            self.assertIn('data-workflow-history-mode="selected"', html)
            self.assertIn('id="workflow-history-toggle"', html)
            self.assertIn('aria-pressed="false"', html)
            self.assertIn("Show all workflows", html)
            self.assertIn("current flat workflow", html)
            self.assertIn("archived canonical workflow", html)
            self.assertIn("missing read model workflow", html)
            self.assertIn("read-only", html)
            self.assertIn("archived", html)
            self.assertIn("unavailable", html)
            self.assertIn("Selection does not update current_workflow.json", html)

            payload = dashboard_script_payload(html)
            self.assertEqual(payload["workspace"]["current_workflow_id"], current_workflow_id)
            self.assertEqual(payload["workspace"]["selected_workflow_id"], current_workflow_id)
            self.assertEqual(len(payload["workflows"]), 3)
            self.assertTrue(payload["workflow_snapshots"][archived_workflow_id]["ok"])
            self.assertEqual(
                payload["workflow_snapshots"][archived_workflow_id]["read_models"]["workflow_status.json"]["status"],
                "archived_view",
            )
            self.assertFalse(payload["workflow_snapshots"][archived_workflow_id]["planning_controls"]["mutation_allowed"])
            self.assertIn("read_only", payload["workflow_snapshots"][archived_workflow_id]["planning_controls"]["mutation_blockers"])
            self.assertFalse(payload["workflow_snapshots"][archived_workflow_id]["execution_controls"]["mutation_allowed"])
            self.assertIn("read_only", payload["workflow_snapshots"][archived_workflow_id]["execution_controls"]["mutation_blockers"])
            self.assertFalse(payload["workflow_snapshots"][archived_workflow_id]["approval_controls"]["mutation_allowed"])
            self.assertIn("read_only", payload["workflow_snapshots"][archived_workflow_id]["approval_controls"]["mutation_blockers"])
            self.assertFalse(payload["workflow_snapshots"][archived_workflow_id]["read_model_rebuild"]["mutation_allowed"])
            self.assertIn("read_only", payload["workflow_snapshots"][archived_workflow_id]["read_model_rebuild"]["mutation_blockers"])
            self.assertFalse(payload["workflow_snapshots"][missing_workflow_id]["ok"])

    def test_static_dashboard_default_does_not_embed_inactive_workflow_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            archived_workflow_id = "wf_static_lightweight_archived"
            write_canonical_read_models(project, archived_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_static_lightweight",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "current static workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                    },
                    {
                        "workflow_id": archived_workflow_id,
                        "name": "archived static workflow",
                        "status": "archived_view",
                        "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                        "read_only": True,
                        "archived": True,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            result = render_static_dashboard(project, output_dir=project / "dashboard_lightweight")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertFalse(result["embed_workflow_snapshots"])
            payload = dashboard_script_payload((project / result["index_file"]).read_text(encoding="utf-8"))
            self.assertNotIn("workflow_snapshots", payload)
            self.assertEqual(payload["workflow_id"], current_workflow_id)
            self.assertEqual(len(payload["workflows"]), 2)

    def test_static_dashboard_defaults_to_current_workflow_when_visualizable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            recent_workflow_id = "wf_20260615_abcdef12"
            missing_workflow_id = "wf_20260615_deadbeef"
            write_canonical_read_models(project, recent_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_recent_selector",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "older current workflow",
                        "status": "completed",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "created_at": "2026-06-15T01:00:00Z",
                        "last_seen_at": "2026-06-15T02:00:00Z",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": recent_workflow_id,
                        "name": "most recent visualizable workflow",
                        "status": "completed",
                        "workflow_root": f".loopplane/workflows/{recent_workflow_id}",
                        "read_models_dir": f".loopplane/workflows/{recent_workflow_id}/read_models",
                        "runtime_dir": f".loopplane/workflows/{recent_workflow_id}/runtime",
                        "requests_dir": f".loopplane/workflows/{recent_workflow_id}/requests",
                        "created_at": "2026-06-15T03:00:00Z",
                        "last_seen_at": "2026-06-15T04:00:00Z",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": missing_workflow_id,
                        "name": "newer but not visualizable workflow",
                        "status": "completed",
                        "workflow_root": f".loopplane/workflows/{missing_workflow_id}",
                        "read_models_dir": f".loopplane/workflows/{missing_workflow_id}/read_models",
                        "runtime_dir": f".loopplane/workflows/{missing_workflow_id}/runtime",
                        "requests_dir": f".loopplane/workflows/{missing_workflow_id}/requests",
                        "created_at": "2026-06-15T05:00:00Z",
                        "last_seen_at": "2026-06-15T06:00:00Z",
                        "read_only": False,
                        "archived": False,
                    },
                ],
            }
            registry_path = project / ".loopplane" / "workflow_registry.json"
            current_path = project / ".loopplane" / "current_workflow.json"
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_recent_selector",
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()

            result = render_static_dashboard(project, output_dir=project / "dashboard_current_default")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["workflow_id"], current_workflow_id)
            self.assertEqual(current_path.read_bytes(), current_before)
            payload = dashboard_script_payload((project / result["index_file"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["workflow_id"], current_workflow_id)
            self.assertEqual(payload["workspace"]["current_workflow_id"], current_workflow_id)
            self.assertEqual(payload["workspace"]["selected_workflow_id"], current_workflow_id)
            self.assertNotIn("workflow_snapshots", payload)

            explicit = render_static_dashboard(
                project,
                output_dir=project / "dashboard_explicit_current",
                workflow_id=current_workflow_id,
            )

            self.assertTrue(explicit["ok"], json.dumps(explicit, indent=2, sort_keys=True))
            self.assertEqual(explicit["workflow_id"], current_workflow_id)
            explicit_payload = dashboard_script_payload((project / explicit["index_file"]).read_text(encoding="utf-8"))
            self.assertEqual(explicit_payload["workspace"]["selected_workflow_id"], current_workflow_id)

    def test_workflow_recency_selection_does_not_parse_jsonl_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            record = {
                "workflow_id": "wf_recency_jsonl_guard",
                "read_models_dir": ".loopplane/read_models",
                "last_seen_at": "2026-06-15T00:00:00Z",
            }
            jsonl_path = project / ".loopplane" / "read_models" / "run_summaries.jsonl"
            jsonl_path.write_text("{not valid json object}\n" * 1024, encoding="utf-8")

            original_read_optional_json = dashboard_module._read_optional_json

            def guarded_read_optional_json(path: Path) -> dict[str, object]:
                self.assertNotEqual(path.suffix, ".jsonl", f"recency selection must not parse {path.name}")
                return original_read_optional_json(path)

            dashboard_module._read_optional_json = guarded_read_optional_json
            try:
                key = dashboard_module._dashboard_workflow_recency_key(project, record, fallback_index=7)
            finally:
                dashboard_module._read_optional_json = original_read_optional_json

            self.assertEqual(key[1], 7)

    def test_read_model_cache_is_scoped_bounded_and_invalidates_by_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            model = project / "workflow_status.json"
            model.write_text(json.dumps({"value": 1}, sort_keys=True) + "\n", encoding="utf-8")
            cache = dashboard_module._ReadModelCache(max_entries=4, max_scopes=2, max_bytes=512)

            first_stats: dict[str, int] = {}
            self.assertEqual(cache.read_json(model, scope="project-a|workflow-a", stats=first_stats)["value"], 1)
            self.assertEqual(first_stats["disk_reads"], 1)
            self.assertEqual(first_stats.get("cache_hits", 0), 0)

            second_stats: dict[str, int] = {}
            self.assertEqual(cache.read_json(model, scope="project-a|workflow-a", stats=second_stats)["value"], 1)
            self.assertEqual(second_stats["cache_hits"], 1)
            self.assertEqual(second_stats.get("disk_reads", 0), 0)

            scoped_stats: dict[str, int] = {}
            self.assertEqual(cache.read_json(model, scope="project-a|workflow-b", stats=scoped_stats)["value"], 1)
            self.assertEqual(scoped_stats["disk_reads"], 1)
            self.assertEqual(scoped_stats.get("cache_hits", 0), 0)

            model.write_text(json.dumps({"value": 22, "padding": "x"}, sort_keys=True) + "\n", encoding="utf-8")
            stale_stats: dict[str, int] = {}
            self.assertEqual(cache.read_json(model, scope="project-a|workflow-a", stats=stale_stats)["value"], 22)
            self.assertEqual(stale_stats["disk_reads"], 1)
            self.assertEqual(stale_stats.get("cache_hits", 0), 0)

            bounded = dashboard_module._ReadModelCache(max_entries=10, max_scopes=1, max_bytes=170)
            for index, scope in enumerate(("scope-a", "scope-b", "scope-c"), start=1):
                path = project / f"model_{index}.json"
                path.write_text(
                    json.dumps({"index": index, "padding": "x" * 80}, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                bounded.read_json(path, scope=scope, stats={})
            snapshot = bounded.snapshot()
            self.assertLessEqual(snapshot["scopes"], 1)
            self.assertLessEqual(snapshot["total_bytes"], 170)

    def test_read_model_cache_defaults_are_tunable_by_environment(self) -> None:
        previous = {
            key: os.environ.get(key)
            for key in (
                "LOOPPLANE_READ_MODEL_CACHE_MAX_ENTRIES",
                "LOOPPLANE_READ_MODEL_CACHE_MAX_SCOPES",
                "LOOPPLANE_READ_MODEL_CACHE_MAX_BYTES",
            )
        }
        try:
            os.environ["LOOPPLANE_READ_MODEL_CACHE_MAX_ENTRIES"] = "7"
            os.environ["LOOPPLANE_READ_MODEL_CACHE_MAX_SCOPES"] = "3"
            os.environ["LOOPPLANE_READ_MODEL_CACHE_MAX_BYTES"] = "2048"

            cache = dashboard_module._ReadModelCache()
            snapshot = cache.snapshot()
            self.assertEqual(snapshot["max_entries"], 7)
            self.assertEqual(snapshot["max_scopes"], 3)
            self.assertEqual(snapshot["max_bytes"], 2048)

            explicit = dashboard_module._ReadModelCache(max_entries=2, max_scopes=1, max_bytes=99).snapshot()
            self.assertEqual(explicit["max_entries"], 2)
            self.assertEqual(explicit["max_scopes"], 1)
            self.assertEqual(explicit["max_bytes"], 99)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_split_run_detail_lookup_does_not_open_other_run_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            write_worker_run(project, run_id="run_other", create_artifact=True)
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))
            manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            other_detail = next(record for record in manifest["runs"] if record["run_id"] == "run_other")
            other_detail_path = (project / other_detail["path"]).resolve()
            workflow_config = load_workflow_config(project)
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }

            original_read_optional_json = dashboard_module._read_optional_json

            def guarded_read_optional_json(path: Path) -> dict[str, object]:
                self.assertNotEqual(Path(path).resolve(), other_detail_path, "run A lookup must not open run B detail")
                return original_read_optional_json(path)

            dashboard_module._read_optional_json = guarded_read_optional_json
            try:
                payload = dashboard_module._run_detail_payload_from_split(context, "run_fixture")
            finally:
                dashboard_module._read_optional_json = original_read_optional_json

            self.assertIsNotNone(payload)
            self.assertEqual(payload["run_id"], "run_fixture")
            self.assertEqual(payload["source"], "run_details")

    def test_missing_split_run_detail_get_does_not_write_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            manifest = json.loads((paths.read_models_dir / "run_details_manifest.json").read_text(encoding="utf-8"))
            detail_entry = next(record for record in manifest["runs"] if record["run_id"] == "run_fixture")
            detail_path = project / detail_entry["path"]
            self.assertTrue(detail_path.is_file())
            detail_path.unlink()
            protected = [paths.read_models_dir / filename for filename in READ_MODEL_FILES]
            before = file_hashes(project, protected)

            payload = dashboard_module._run_detail_payload(context, "run_fixture")

            after = file_hashes(project, protected)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["status"], "detail_missing")
            self.assertEqual(payload["run_id"], "run_fixture")
            self.assertEqual(after, before)

    def test_selected_dashboard_data_does_not_open_split_run_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            (paths.read_models_dir / "run_details_manifest.json").write_text("{not valid json\n", encoding="utf-8")
            (paths.read_models_dir / "build_manifest.json").write_text("{not valid json\n", encoding="utf-8")
            detail_root = (paths.read_models_dir / "run_details").resolve()
            self.assertTrue(detail_root.is_dir())
            original_read_optional_json = dashboard_module._read_optional_json

            def guarded_read_optional_json(path: Path) -> dict[str, object]:
                resolved = Path(path).resolve()
                if resolved != detail_root and detail_root in resolved.parents:
                    raise AssertionError("selected dashboard data must not open split run detail files")
                return original_read_optional_json(path)

            dashboard_module._read_optional_json = guarded_read_optional_json
            try:
                payload = dashboard_module._dashboard_data_response(context)
            finally:
                dashboard_module._read_optional_json = original_read_optional_json

            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertIn("run_index.jsonl", payload["jsonl_models"])
            self.assertNotIn("run_summaries.jsonl", payload["jsonl_models"])
            self.assertNotIn("run_details_manifest.json", payload["read_models"])
            self.assertNotIn("build_manifest.json", payload["read_models"])
            self.assertNotIn("run_details_manifest.json", payload["read_model_freshness"]["checked_files"])
            self.assertNotIn("build_manifest.json", payload["read_model_freshness"]["checked_files"])
            self.assertNotIn("run_details_manifest.json", dashboard_module._dashboard_data_read_model_filenames(paths))
            self.assertNotIn("build_manifest.json", dashboard_module._dashboard_data_read_model_filenames(paths))

    def test_selected_dashboard_data_skips_legacy_run_summaries_when_split_index_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            (paths.read_models_dir / "run_index.jsonl").unlink()
            legacy_path = paths.read_models_dir / "run_summaries.jsonl"
            legacy_path.write_text('{"legacy": true, "padding": "' + ("x" * 2048) + '"}\n', encoding="utf-8")
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            original_read_jsonl = dashboard_module._read_jsonl

            def guarded_read_jsonl(path: Path) -> list[dict[str, object]]:
                if Path(path).resolve() == legacy_path.resolve():
                    raise AssertionError("live selected dashboard data must not read legacy run_summaries.jsonl")
                return original_read_jsonl(path)

            dashboard_module._read_jsonl = guarded_read_jsonl
            try:
                payload = dashboard_module._dashboard_data_response(context)
            finally:
                dashboard_module._read_jsonl = original_read_jsonl

            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertNotIn("run_summaries.jsonl", payload["jsonl_models"])
            self.assertNotIn("run_index.jsonl", payload["jsonl_models"])
            self.assertEqual(
                payload["read_model_diagnostics"]["read_models"]["skipped_legacy_run_summaries"],
                1,
            )

    def test_selected_dashboard_data_uses_split_index_without_reading_legacy_run_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            legacy_path = paths.read_models_dir / "run_summaries.jsonl"
            legacy_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.5",
                        "workflow_id": workflow_config["workflow_id"],
                        "generated_at": "2026-06-24T00:00:00Z",
                        "source_hashes": {},
                        "run_id": "run_heavy_legacy",
                        "node_id": "node_run_heavy_legacy",
                        "detail_status": "available",
                        "details": {"padding": "x" * 4096},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            original_read_jsonl = dashboard_module._read_jsonl

            def guarded_read_jsonl(path: Path) -> list[dict[str, object]]:
                if Path(path).resolve() == legacy_path.resolve():
                    raise AssertionError("selected dashboard data must use run_index.jsonl without reading legacy run_summaries.jsonl")
                return original_read_jsonl(path)

            dashboard_module._read_jsonl = guarded_read_jsonl
            try:
                payload = dashboard_module._dashboard_data_response(context)
            finally:
                dashboard_module._read_jsonl = original_read_jsonl

            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertIn("run_index.jsonl", payload["jsonl_models"])
            self.assertNotIn("run_summaries.jsonl", payload["jsonl_models"])
            self.assertEqual(
                payload["read_model_diagnostics"]["read_models"]["skipped_legacy_run_summaries"],
                1,
            )

    def test_dashboard_benchmark_cli_reports_bounded_loading_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "benchmark",
                    "--project",
                    str(project),
                    "--iterations",
                    "1",
                    "--warmups",
                    "0",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "benchmarked")
            operations = payload["operations"]
            self.assertIn("dashboard_shell", operations)
            self.assertIn("dashboard_data_etag", operations)
            self.assertIn("selected_dashboard_data_cold_cache", operations)
            self.assertIn("selected_dashboard_data_warm_cache", operations)
            shell_sample = operations["dashboard_shell"]["samples"][0]
            cold_sample = operations["selected_dashboard_data_cold_cache"]["samples"][0]
            self.assertEqual(shell_sample["json_model_count"], 0)
            self.assertEqual(shell_sample["jsonl_model_count"], 0)
            self.assertGreater(cold_sample["payload_bytes"], 0)
            self.assertIn("read_model_disk_read_bytes", cold_sample)
            for operation in operations.values():
                for sample in operation["samples"]:
                    self.assertNotIn("read_models", sample)
                    self.assertNotIn("jsonl_models", sample)

    def test_dashboard_data_etag_does_not_parse_plan_index_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            original_read_optional_json = dashboard_module._read_optional_json

            def guarded_read_optional_json(path: Path) -> dict[str, object]:
                if Path(path).resolve() == (paths.read_models_dir / "plan_index.json").resolve():
                    raise AssertionError("dashboard-data ETag must not parse plan_index.json")
                return original_read_optional_json(path)

            dashboard_module._read_optional_json = guarded_read_optional_json
            try:
                etag = dashboard_module._dashboard_data_etag(context)
            finally:
                dashboard_module._read_optional_json = original_read_optional_json

            self.assertTrue(etag.startswith('"dashboard-'))

    def test_dashboard_data_etag_uses_bounded_legacy_active_lease_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow_config = load_workflow_config(project)
            lease_dir = paths.runtime_dir / "active_run_leases"
            lease_dir.mkdir(parents=True, exist_ok=True)
            stamp_path = paths.runtime_dir / dashboard_module.ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME
            if stamp_path.exists():
                stamp_path.unlink()
            existing_count = len(list(lease_dir.glob("*.json")))
            for index in range(80):
                (lease_dir / f"run_legacy_{index:03d}.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "1.5",
                            "workflow_id": workflow_config["workflow_id"],
                            "run_id": f"run_legacy_{index:03d}",
                            "status": "completed",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            context = {
                "project": project,
                "paths": paths,
                "workflow_id": workflow_config["workflow_id"],
            }
            original_path_stat_fingerprint = dashboard_module._path_stat_fingerprint

            def guarded_path_stat_fingerprint(root: Path, path: Path) -> dict[str, object]:
                resolved = Path(path).resolve()
                if resolved.parent == lease_dir.resolve() and resolved.suffix == ".json":
                    raise AssertionError("dashboard-data ETag must not stat each legacy active-run lease")
                return original_path_stat_fingerprint(root, path)

            dashboard_module._path_stat_fingerprint = guarded_path_stat_fingerprint
            try:
                fingerprint = dashboard_module._active_run_leases_etag_fingerprint(project, paths)
                etag = dashboard_module._dashboard_data_etag(context)
            finally:
                dashboard_module._path_stat_fingerprint = original_path_stat_fingerprint

            self.assertEqual(fingerprint["mode"], "bounded_legacy_directory")
            self.assertEqual(fingerprint["legacy_files"]["file_count"], existing_count + 80)
            self.assertTrue(fingerprint["legacy_files"]["truncated"])
            self.assertTrue(etag.startswith('"dashboard-'))

    def test_dashboard_data_etag_uses_active_lease_stamp_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            stamp_path = paths.runtime_dir / dashboard_module.ACTIVE_RUN_LEASE_FINGERPRINT_FILENAME
            stamp_path.write_text('{"schema_version":"1.5","update_id":"stamp"}\n', encoding="utf-8")
            original_bounded = dashboard_module._bounded_directory_name_fingerprints

            def fail_bounded(*_args: object, **_kwargs: object) -> dict[str, object]:
                raise AssertionError("stamp mode must not scan legacy active-run lease names")

            dashboard_module._bounded_directory_name_fingerprints = fail_bounded
            try:
                fingerprint = dashboard_module._active_run_leases_etag_fingerprint(project, paths)
            finally:
                dashboard_module._bounded_directory_name_fingerprints = original_bounded

            self.assertEqual(fingerprint["mode"], "stamp")
            self.assertTrue(fingerprint["stamp"]["exists"])

    def test_server_index_shell_does_not_load_selected_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            original_load_read_models = dashboard_module._load_read_models

            def fail_if_read_models_loaded(*args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("server index shell must not load selected workflow read models")

            dashboard_module._load_read_models = fail_if_read_models_loaded
            try:
                prepared = dashboard_module._prepare_dashboard_shell_payload(project)
            finally:
                dashboard_module._load_read_models = original_load_read_models

            self.assertTrue(prepared["ok"], json.dumps(dashboard_module._public_result(prepared), indent=2, sort_keys=True))
            payload = prepared["_payload"]
            self.assertTrue(payload["initial_dashboard_load"])
            self.assertEqual(payload["read_models"], {})
            self.assertEqual(payload["jsonl_models"], {})
            self.assertIn("workflow_summaries", payload)
            self.assertNotIn("workflow_snapshots", payload)

    def test_dashboard_freshness_falls_back_to_event_tail_when_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            manifest_path = paths.runtime_dir / "events" / "manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest_path.unlink()

            reference = dashboard_module._event_log_reference(paths)

            self.assertEqual(reference["freshness_mode"], "event_tail")
            self.assertIsNotNone(reference["source_event_id"])

    def test_static_dashboard_runner_config_is_read_only_until_trusted_local_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            write_runner_boundary_fixture(project)
            set_dashboard_trusted_local(project, False)

            result = render_static_dashboard(project, output_dir=project / "dashboard_runner_readonly")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("runner_configuration", result["covered_sections"])
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Runner Configuration", html)
            self.assertIn("Trusted local mode is disabled", html)
            self.assertIn("worker", html)
            self.assertIn("[hidden until trusted local mode]", html)
            self.assertNotIn("/home/alice/.local/bin/codex", html)
            self.assertNotIn("runner-secret-123456", html)
            self.assertNotIn("runner-config-request-form", html)

            payload = dashboard_script_payload(html)
            runner_config = payload["runner_configuration"]
            self.assertFalse(runner_config["trusted_local_mode"])
            self.assertFalse(runner_config["configuration_requests_allowed"])
            self.assertEqual(runner_config["runners"][0]["command"], "[hidden until trusted local mode]")
            self.assertNotIn("env", json.dumps(runner_config, sort_keys=True))
            self.assertNotIn("runner-secret-123456", json.dumps(payload, sort_keys=True))

            set_dashboard_trusted_local(project, True)
            trusted = render_static_dashboard(project, output_dir=project / "dashboard_runner_trusted")

            self.assertTrue(trusted["ok"], json.dumps(trusted, indent=2, sort_keys=True))
            trusted_html = (project / trusted["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Runner Configuration", trusted_html)
            self.assertIn("Trusted local mode is enabled", trusted_html)
            self.assertIn("[local path redacted]/codex", trusted_html)
            self.assertIn("Role", trusted_html)
            self.assertIn("Adapter", trusted_html)
            self.assertIn("Command", trusted_html)
            self.assertIn("Model", trusted_html)
            self.assertIn("Effort", trusted_html)
            self.assertIn("Prompt", trusted_html)
            self.assertIn("Timeout", trusted_html)
            self.assertIn("Doctor", trusted_html)
            self.assertNotIn("/home/alice/.local/bin/codex", trusted_html)
            self.assertNotIn("runner-secret-123456", trusted_html)
            self.assertNotIn("runner-config-request-form", trusted_html)

            trusted_payload = dashboard_script_payload(trusted_html)
            trusted_runner = trusted_payload["runner_configuration"]["runners"][0]
            self.assertTrue(trusted_payload["runner_configuration"]["trusted_local_mode"])
            self.assertEqual(trusted_runner["runner_id"], "worker")
            self.assertEqual(trusted_runner["doctor"]["status"], "not_run")
            self.assertIn("diagnostics", trusted_runner["doctor"])

    def test_static_dashboard_renders_approval_panel_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            enable_approvals(project)
            evidence_path = project / ".loopplane" / "results" / "T001" / "latest.json"
            append_pending_approval(
                project,
                workflow_id,
                "approval_static_panel",
                message="Approve static panel fixture.",
                evidence_path=evidence_path.as_posix(),
                source_path="/home/alice/private/secret-plan.md",
            )

            result = render_static_dashboard(project, output_dir=project / "dashboard_approval_panel")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertIn("approval_panel", result["covered_sections"])
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Approval Panel", html)
            self.assertIn("approval_static_panel", html)
            self.assertIn("Approve static panel fixture.", html)
            self.assertIn("1 pending approval request", html)
            self.assertIn("Static dashboard is read-only", html)
            self.assertIn("approval-response-form", html)
            self.assertIn("data-approval-decision=\"approved\" disabled", html)
            self.assertIn("data-approval-decision=\"rejected\" disabled", html)
            self.assertIn(".loopplane/results/T001/latest.json", html)
            self.assertIn("[redacted path]", html)
            self.assertNotIn("/home/alice/private/secret-plan.md", html)

            payload = dashboard_script_payload(html)
            approvals = payload["approval_controls"]
            self.assertEqual(approvals["pending_count"], 1)
            self.assertTrue(approvals["mutation_allowed"])
            self.assertEqual(approvals["pending"][0]["approval_id"], "approval_static_panel")
            self.assertIn(".loopplane/results/T001/latest.json", approvals["pending"][0]["evidence_refs"])
            self.assertIn("[redacted path]", approvals["pending"][0]["evidence_refs"])

    def test_cli_generates_static_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            output = project / "dashboard_out"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--output",
                    str(output),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["index_file"], "dashboard_out/index.html")
            self.assertTrue((output / "index.html").is_file())
            self.assertTrue((output / "read_models" / "workflow_status.json").is_file())

    def test_cli_dashboard_workflow_selects_static_read_models_without_pointer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            selected_workflow_id = "wf_cli_dashboard_selected"
            write_canonical_read_models(project, selected_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_cli_dashboard_select",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "current workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": selected_workflow_id,
                        "name": "selected archived workflow",
                        "status": "archived",
                        "workflow_root": f".loopplane/workflows/{selected_workflow_id}",
                        "read_only": True,
                        "archived": True,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_cli_dashboard_select",
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()
            output = project / "dashboard_selected"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--workflow",
                    selected_workflow_id,
                    "--output",
                    str(output),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["workflow_id"], selected_workflow_id)
            self.assertEqual(result["read_models_dir"], f".loopplane/workflows/{selected_workflow_id}/read_models")
            self.assertEqual(current_path.read_bytes(), current_before)
            self.assertTrue((output / "read_models" / "workflow_status.json").is_file())

            html = (output / "index.html").read_text(encoding="utf-8")
            payload = dashboard_script_payload(html)
            self.assertEqual(payload["workflow_id"], selected_workflow_id)
            self.assertEqual(payload["workspace"]["current_workflow_id"], current_workflow_id)
            self.assertEqual(payload["workspace"]["selected_workflow_id"], selected_workflow_id)
            self.assertEqual(payload["read_models"]["workflow_status.json"]["workflow_id"], selected_workflow_id)
            self.assertEqual(payload["read_models"]["workflow_status.json"]["status"], "archived_view")
            self.assertFalse(payload["planning_controls"]["mutation_allowed"])
            self.assertIn("archived", payload["planning_controls"]["mutation_blockers"])
            self.assertFalse(payload["execution_controls"]["mutation_allowed"])
            self.assertIn("read_only", payload["execution_controls"]["mutation_blockers"])
            self.assertFalse(payload["approval_controls"]["mutation_allowed"])
            self.assertFalse(payload["read_model_rebuild"]["mutation_allowed"])

    def test_cli_dashboard_workflow_rejects_unknown_registry_id_without_pointer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current_path = project / ".loopplane" / "current_workflow.json"
            current_before = current_path.read_bytes()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--workflow",
                    "wf_20260611_deadbeef",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            result = json.loads(completed.stdout)
            self.assertFalse(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            self.assertEqual(result["status"], "failed")
            self.assertIn("not registered", "\n".join(result["errors"]))
            self.assertEqual(current_path.read_bytes(), current_before)

    def test_cli_dashboard_workflow_selects_server_context_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            workspace_id = workspace["workspace_id"]
            selected_workflow_id = "wf_cli_dashboard_server"
            write_canonical_read_models(project, selected_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": workspace_id,
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "server current workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": selected_workflow_id,
                        "name": "server selected workflow",
                        "status": "stopped",
                        "workflow_root": f".loopplane/workflows/{selected_workflow_id}",
                        "read_only": False,
                        "archived": False,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": workspace_id,
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--workflow",
                    selected_workflow_id,
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["workflow_id"], selected_workflow_id)
                self.assertEqual(startup["selected_workflow_id"], selected_workflow_id)
                self.assertEqual(startup["current_workflow_id"], current_workflow_id)
                self.assertEqual(startup["workspace_id"], workspace_id)
                self.assertEqual(startup["selection_scope"], "dashboard_visualization_only")
                server_state = json.loads((project / str(startup["server_state_file"])).read_text(encoding="utf-8"))
                self.assertEqual(server_state["workflow_id"], selected_workflow_id)
                self.assertEqual(server_state["current_workflow_id"], current_workflow_id)

                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"
                health = request_json(f"{base}/api/health", token)
                self.assertEqual(health["selected_workflow_id"], selected_workflow_id)
                self.assertEqual(health["current_workflow_id"], current_workflow_id)
                read_model = request_json(f"{base}/read_models/workflow_status.json", token)
                self.assertEqual(read_model["workflow_id"], selected_workflow_id)
                dashboard_data = request_json(f"{base}/api/workflows/{selected_workflow_id}/dashboard-data", token)
                self.assertEqual(dashboard_data["workflow_id"], selected_workflow_id)
                self.assertEqual(dashboard_data["workspace"]["current_workflow_id"], current_workflow_id)
                self.assertEqual(dashboard_data["workspace"]["selected_workflow_id"], selected_workflow_id)
                self.assertEqual(current_path.read_bytes(), current_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_list_reports_empty_state_and_selector_metadata_without_pointer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current_path = project / ".loopplane" / "current_workflow.json"
            current_before = current_path.read_bytes()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "list",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "no_active_dashboard")
            self.assertEqual(payload["server_record_count"], 0)
            self.assertEqual(payload["active_server_count"], 0)
            self.assertEqual(payload["workflow_count"], 1)
            workflow = payload["workflows"][0]
            self.assertEqual(workflow["labels"], ["current"])
            self.assertIn("workflow_id", workflow)
            self.assertIn("name", workflow)
            self.assertIn("status", workflow)
            self.assertIn("created_at", workflow)
            self.assertIn("last_seen_at", workflow)
            self.assertIn("progress_summary", workflow)
            self.assertIn("completion_freshness", workflow)
            self.assertIn("git_checkpoint_status", workflow)
            self.assertIn("runtime_health", workflow)
            self.assertIn(workflow["runtime_health"]["status"], {"healthy", "degraded", "unhealthy"})
            self.assertEqual(workflow["dashboard_server"]["status"], "missing")
            self.assertEqual(current_path.read_bytes(), current_before)

            text = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "list",
                    "--project",
                    str(project),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(text.returncode, 0, text.stderr + text.stdout)
            self.assertIn("loopplane dashboard list: no_active_dashboard", text.stdout)
            self.assertIn("dashboard_servers: none", text.stdout)
            self.assertIn("runtime_health:", text.stdout)
            self.assertIn("mutation_boundary:", text.stdout)

    def test_cli_dashboard_list_discovers_configured_v16_server_state_and_redacts_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            selected_workflow_id = "wf_20260611_abcd1234"
            selected_root = write_canonical_read_models(project, selected_workflow_id)
            custom_state = selected_root / "runtime" / "custom" / "dashboard_server.json"
            custom_state.parent.mkdir(parents=True, exist_ok=True)
            (selected_root / "config" / "dashboard.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": "auto",
                        "server_state_file": "{{workflow_root}}/runtime/custom/dashboard_server.json",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            custom_state.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "ok": True,
                        "status": "serving",
                        "server_mode": True,
                        "project_root": project.as_posix(),
                        "workspace_id": workspace["workspace_id"],
                        "workflow_id": selected_workflow_id,
                        "selected_workflow_id": selected_workflow_id,
                        "current_workflow_id": current_workflow_id,
                        "selection_scope": "dashboard_visualization_only",
                        "started_at": "2026-06-11T00:00:00Z",
                        "host": "127.0.0.1",
                        "port": 3766,
                        "url": "http://127.0.0.1:3766/",
                        "api_base_url": "http://127.0.0.1:3766/api",
                        "pid": os.getpid(),
                        "token": "super-secret-dashboard-token",
                        "token_file": "/tmp/outside-dashboard-token",
                        "server_state_file": custom_state.as_posix(),
                        "token_required": True,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            registry = {
                "schema_version": "1.6",
                "workspace_id": workspace["workspace_id"],
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "dashboard list current",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "read_only": False,
                        "archived": False,
                        "summary": {
                            "one_line": "Current dashboard list fixture.",
                            "tasks_total": 1,
                            "tasks_completed": 1,
                            "tasks_blocked": 0,
                        },
                    },
                    {
                        "workflow_id": selected_workflow_id,
                        "name": "dashboard list selected",
                        "status": "archived",
                        "workflow_root": f".loopplane/workflows/{selected_workflow_id}",
                        "read_only": True,
                        "archived": True,
                        "summary": {
                            "one_line": "Archived dashboard list fixture.",
                            "tasks_total": 1,
                            "tasks_completed": 0,
                            "tasks_blocked": 0,
                        },
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": workspace["workspace_id"],
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "list",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "listed")
            self.assertEqual(payload["server_record_count"], 1)
            self.assertEqual(payload["active_server_count"], 1)
            self.assertEqual([server["workflow_id"] for server in payload["dashboard_servers"]], [selected_workflow_id])
            selected = next(workflow for workflow in payload["workflows"] if workflow["workflow_id"] == selected_workflow_id)
            self.assertEqual(selected["labels"], ["archived", "read_only"])
            self.assertEqual(selected["progress_summary"]["tasks_total"], 1)
            self.assertEqual(selected["completion_freshness"]["status"], "missing")
            self.assertEqual(selected["git_checkpoint_status"]["status"], "unavailable")
            server = selected["dashboard_server"]
            self.assertEqual(server["state_file"], f".loopplane/workflows/{selected_workflow_id}/runtime/custom/dashboard_server.json")
            self.assertEqual(server["url"], "http://127.0.0.1:3766/")
            self.assertEqual(server["host"], "127.0.0.1")
            self.assertEqual(server["port"], 3766)
            self.assertEqual(server["pid"], os.getpid())
            self.assertEqual(server["selected_workflow_id"], selected_workflow_id)
            self.assertEqual(server["current_workflow_id"], current_workflow_id)
            self.assertEqual(server["liveness"], "alive")
            self.assertFalse(server["stale"])
            self.assertEqual(server["token_file"], "[redacted path]")
            server_state_json = json.dumps(server["server_state"], sort_keys=True)
            self.assertIn("[REDACTED]", server_state_json)
            self.assertNotIn("super-secret-dashboard-token", server_state_json)
            self.assertNotIn("/tmp/outside-dashboard-token", json.dumps(payload, sort_keys=True))
            self.assertEqual(current_path.read_bytes(), current_before)

    def test_cli_dashboard_port_auto_records_allocated_port_and_server_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    "auto",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertIsInstance(startup["port"], int)
                self.assertGreaterEqual(startup["port"], 1)
                self.assertLessEqual(startup["port"], 65535)
                server_state = json.loads((project / str(startup["server_state_file"])).read_text(encoding="utf-8"))
                self.assertEqual(server_state["port"], startup["port"])
                self.assertEqual(server_state["host"], "127.0.0.1")
                self.assertEqual(server_state["url"], startup["url"])
                self.assertEqual(server_state["token_file"], startup["token_file"])
                self.assertIsInstance(server_state["pid"], int)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_json_server_closes_stdout_after_startup_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    "auto",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertIsNone(process.poll())
                self.assertIsNotNone(process.stdout)
                ready, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(ready, "dashboard --json stdout did not close after startup JSON")
                self.assertEqual(process.stdout.readline(), "")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_cleanup_removes_stale_project_and_home_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            home = root / "loopplane_home"
            env = {**os.environ, "LOOPPLANE_HOME": str(home)}
            prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            state_file = project / ".loopplane" / "runtime" / "dashboard_server.json"
            state_file.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "ok": True,
                        "status": "serving",
                        "server_mode": True,
                        "project_root": project.as_posix(),
                        "workspace_id": workspace["workspace_id"],
                        "workflow_id": workflow["workflow_id"],
                        "selected_workflow_id": workflow["workflow_id"],
                        "current_workflow_id": workflow["workflow_id"],
                        "started_at": "2026-06-13T00:00:00Z",
                        "host": "127.0.0.1",
                        "port": 3777,
                        "url": "http://127.0.0.1:3777/",
                        "pid": 99999999,
                        "server_state_file": ".loopplane/runtime/dashboard_server.json",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            home_servers = home / "dashboard" / "servers.json"
            home_servers.parent.mkdir(parents=True, exist_ok=True)
            home_servers.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "authority": "loopplane_home",
                        "servers": [
                            {
                                "schema_version": "1.6",
                                "authority": "loopplane_home",
                                "record_type": "dashboard_server",
                                "status": "serving",
                                "server_mode": True,
                                "workspace_id": workspace["workspace_id"],
                                "workflow_id": workflow["workflow_id"],
                                "project_root": project.as_posix(),
                                "server_state_file": ".loopplane/runtime/dashboard_server.json",
                                "host": "127.0.0.1",
                                "port": 3777,
                                "url": "http://127.0.0.1:3777/",
                                "pid": 99999999,
                                "started_at": "2026-06-13T00:00:00Z",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            cleanup = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "cleanup",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

            self.assertEqual(cleanup.returncode, 0, cleanup.stderr + cleanup.stdout)
            payload = json.loads(cleanup.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["removed_project_record_count"], 1)
            self.assertEqual(payload["removed_loopplane_home_record_count"], 1)
            self.assertFalse(state_file.exists())
            updated_home = json.loads(home_servers.read_text(encoding="utf-8"))
            self.assertEqual(updated_home["servers"], [])

    def test_cli_read_only_project_command_does_not_auto_start_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            env = os.environ.copy()
            env["LOOPPLANE_AUTO_DASHBOARD"] = "1"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "status",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            status_payload = json.loads(completed.stdout)
            self.assertEqual(status_payload["project_root"], project.resolve().as_posix())

            url_file = project / "LOOPPLANE_DASHBOARD.url"
            state_file = project / ".loopplane" / "runtime" / "dashboard_server.json"
            self.assertFalse(url_file.exists())
            self.assertFalse(state_file.exists())

    def test_cli_mutating_project_command_auto_starts_dashboard_and_writes_workspace_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            env = os.environ.copy()
            env["LOOPPLANE_AUTO_DASHBOARD"] = "1"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "write-brief",
                    "--project",
                    str(project),
                    "--stdin",
                    "--force",
                    "--json",
                ],
                input="Updated dashboard auto-start brief.\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            url_file = project / "LOOPPLANE_DASHBOARD.url"
            state_file = project / ".loopplane" / "runtime" / "dashboard_server.json"
            wait_for_file(url_file)
            wait_for_file(state_file)
            state = json.loads(state_file.read_text(encoding="utf-8"))
            try:
                self.assertEqual(state["status"], "serving")
                self.assertEqual(state["server_mode"], True)
                token = (project / str(state["token_file"])).read_text(encoding="utf-8").strip()
                url_text = url_file.read_text(encoding="utf-8")
                self.assertIn("[InternetShortcut]", url_text)
                self.assertIn(f"http://127.0.0.1:{state['port']}/?token=", url_text)
                self.assertIn(token, url_text)
                health = request_json(f"http://127.0.0.1:{state['port']}/api/health", token)
                self.assertTrue(health["ok"], json.dumps(health, indent=2, sort_keys=True))
            finally:
                terminate_pid(state.get("pid"))

    def test_cli_mutating_project_command_noninteractive_does_not_auto_start_dashboard_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "write-brief",
                    "--project",
                    str(project),
                    "--stdin",
                    "--force",
                    "--json",
                ],
                input="Noninteractive dashboard auto-start should stay off by default.\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            self.assertFalse((project / "LOOPPLANE_DASHBOARD.url").exists())

    def test_cli_auto_dashboard_reuses_one_workspace_server_across_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            env = os.environ.copy()
            env["LOOPPLANE_AUTO_DASHBOARD"] = "1"
            env["LOOPPLANE_HOME"] = (Path(tmp) / "loopplane_home").as_posix()

            first = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "write-brief",
                    "--project",
                    str(project),
                    "--stdin",
                    "--force",
                    "--json",
                ],
                input="Auto dashboard should start once.\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            url_file = project / "LOOPPLANE_DASHBOARD.url"
            wait_for_file(url_file)
            before = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "list",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )
            self.assertEqual(before.returncode, 0, before.stderr + before.stdout)
            before_payload = json.loads(before.stdout)
            before_alive = [
                server
                for server in before_payload["dashboard_servers"]
                if server.get("liveness") == "alive" and server.get("stale") is False
            ]
            self.assertEqual(len(before_alive), 1, json.dumps(before_payload, indent=2, sort_keys=True))

            created = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "workflow",
                    "create",
                    "--project",
                    str(project),
                    "--brief",
                    "Second workflow should reuse the existing dashboard server.",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env=env,
            )

            after_payload: dict[str, object] | None = None
            try:
                self.assertEqual(created.returncode, 0, created.stderr + created.stdout)
                created_payload = json.loads(created.stdout)
                after = subprocess.run(
                    [
                        sys.executable,
                        str(LoopPlane),
                        "dashboard",
                        "list",
                        "--project",
                        str(project),
                        "--json",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    env=env,
                )
                self.assertEqual(after.returncode, 0, after.stderr + after.stdout)
                after_payload = json.loads(after.stdout)
                after_alive = [
                    server
                    for server in after_payload["dashboard_servers"]
                    if server.get("liveness") == "alive" and server.get("stale") is False
                ]
                self.assertEqual(len(after_alive), 1, json.dumps(after_payload, indent=2, sort_keys=True))
                self.assertEqual(after_alive[0]["pid"], before_alive[0]["pid"])
                self.assertEqual(after_alive[0]["port"], before_alive[0]["port"])
                url_text = url_file.read_text(encoding="utf-8")
                self.assertIn(f"workflow={quote(str(created_payload['workflow_id']), safe='')}", url_text)
                self.assertIn(f"http://127.0.0.1:{before_alive[0]['port']}/?token=", url_text)
            finally:
                payload = after_payload or before_payload
                for server in payload.get("dashboard_servers", []):
                    if isinstance(server, dict):
                        terminate_pid(server.get("pid"))

    def test_cmdline_dashboard_detection_ignores_control_commands(self) -> None:
        serving = [
            sys.executable,
            str(LoopPlane),
            "dashboard",
            "--project",
            "/tmp/project",
            "--port",
            "auto",
            "--json",
        ]
        cleanup = [
            sys.executable,
            str(LoopPlane),
            "dashboard",
            "cleanup",
            "--project",
            "/tmp/project",
            "--terminate-orphans",
        ]
        listed = [sys.executable, str(LoopPlane), "dashboard", "list", "--project", "/tmp/project"]

        self.assertTrue(_cmdline_is_loopplane_dashboard(serving))
        self.assertFalse(_cmdline_is_loopplane_dashboard(cleanup))
        self.assertFalse(_cmdline_is_loopplane_dashboard(listed))

    def test_cli_dashboard_server_records_machine_local_index_in_loopplane_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            layout = loopplane_home_layout()
            workspace_truth = [
                project / ".loopplane" / "workspace.json",
                project / ".loopplane" / "workflow_registry.json",
                project / ".loopplane" / "current_workflow.json",
            ]
            before_hashes = file_hashes(project, workspace_truth)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    "auto",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            startup: dict[str, object]
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["loopplane_home"], layout.home.as_posix())
                self.assertEqual(startup["loopplane_home_server_record_status"], "recorded")
                self.assertEqual(startup["loopplane_home_dashboard_servers_file"], layout.dashboard_servers_file.as_posix())

                project_state = json.loads((project / str(startup["server_state_file"])).read_text(encoding="utf-8"))
                self.assertEqual(project_state["token_file"], startup["token_file"])
                self.assertIsNone(project_state["token"])
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()

                home_payload = json.loads(layout.dashboard_servers_file.read_text(encoding="utf-8"))
                self.assertEqual(home_payload["authority"], "discovery_only")
                self.assertEqual(home_payload["schema_version"], "1.6")
                self.assertEqual(len(home_payload["servers"]), 1)
                home_record = home_payload["servers"][0]
                self.assertEqual(home_record["workspace_id"], startup["workspace_id"])
                self.assertEqual(home_record["workflow_id"], startup["workflow_id"])
                self.assertEqual(home_record["project_root"], project.as_posix())
                self.assertEqual(home_record["server_state_file"], startup["server_state_file"])
                self.assertEqual(home_record["port"], startup["port"])
                self.assertEqual(home_record["url"], startup["url"])
                self.assertNotIn("token", home_record)
                self.assertNotIn("token_file", home_record)
                self.assertNotIn(token, json.dumps(home_payload, sort_keys=True))

                listed = subprocess.run(
                    [
                        sys.executable,
                        str(LoopPlane),
                        "dashboard",
                        "list",
                        "--project",
                        str(project),
                        "--json",
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(listed.returncode, 0, listed.stderr + listed.stdout)
                list_payload = json.loads(listed.stdout)
                self.assertEqual(list_payload["loopplane_home_dashboard_servers_file"], layout.dashboard_servers_file.as_posix())
                self.assertEqual(list_payload["loopplane_home_server_record_count"], 1)
                self.assertEqual(list_payload["loopplane_home_active_server_count"], 1)
                listed_home_record = list_payload["loopplane_home_dashboard_servers"][0]
                self.assertEqual(listed_home_record["liveness"], "alive")
                self.assertFalse(listed_home_record["stale"])
                self.assertNotIn("token_file", listed_home_record)
                self.assertNotIn(token, json.dumps(list_payload, sort_keys=True))
                self.assertEqual(file_hashes(project, workspace_truth), before_hashes)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

            stale = subprocess.run(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "list",
                    "--project",
                    str(project),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(stale.returncode, 0, stale.stderr + stale.stdout)
            stale_payload = json.loads(stale.stdout)
            stale_home_record = stale_payload["loopplane_home_dashboard_servers"][0]
            self.assertEqual(stale_home_record["liveness"], "dead")
            self.assertTrue(stale_home_record["stale"])
            self.assertIn("pid_not_running", stale_home_record["stale_reasons"])
            self.assertIn("Restart the dashboard", stale_home_record["health_guidance"])
            self.assertEqual(file_hashes(project, workspace_truth), before_hashes)

    def test_cli_dashboard_port_auto_uses_configured_preferred_port_before_range_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            base_port = free_local_port_range(4)
            preferred_port = base_port + 2
            blocker = reserve_local_port(base_port)
            update_dashboard_port_config(
                project / ".loopplane" / "config" / "dashboard.json",
                preferred_port=preferred_port,
                port_range=[base_port, base_port + 3],
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    "auto",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["port"], preferred_port)
                server_state = json.loads((project / str(startup["server_state_file"])).read_text(encoding="utf-8"))
                self.assertEqual(server_state["port"], preferred_port)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                blocker.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_port_auto_skips_occupied_port_and_preserves_workflow_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            selected_workflow_id = "wf_cli_dashboard_auto_selected"
            selected_root = write_canonical_read_models(project, selected_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": workspace["workspace_id"],
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "auto current workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": selected_workflow_id,
                        "name": "auto selected workflow",
                        "status": "stopped",
                        "workflow_root": f".loopplane/workflows/{selected_workflow_id}",
                        "read_only": False,
                        "archived": False,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": workspace["workspace_id"],
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()
            base_port = free_local_port_range(3)
            blocker = reserve_local_port(base_port)
            update_dashboard_port_config(
                selected_root / "config" / "dashboard.json",
                preferred_port=base_port,
                port_range=[base_port, base_port + 2],
                server_state_file=f".loopplane/workflows/{selected_workflow_id}/runtime/dashboard_server.json",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--workflow",
                    selected_workflow_id,
                    "--port",
                    "auto",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["port"], base_port + 1)
                self.assertEqual(startup["workflow_id"], selected_workflow_id)
                self.assertEqual(startup["selected_workflow_id"], selected_workflow_id)
                self.assertEqual(startup["current_workflow_id"], current_workflow_id)
                self.assertEqual(current_path.read_bytes(), current_before)
                server_state = json.loads((project / str(startup["server_state_file"])).read_text(encoding="utf-8"))
                self.assertEqual(server_state["port"], base_port + 1)
                self.assertEqual(server_state["workflow_id"], selected_workflow_id)
                self.assertEqual(server_state["current_workflow_id"], current_workflow_id)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                blocker.close()
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_fixed_port_3766_serves_static_assets_and_core_api(self) -> None:
        if not local_port_available(3766):
            self.skipTest("127.0.0.1:3766 is already in use")
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    "3766",
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["status"], "serving")
                self.assertEqual(startup["port"], 3766)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = "http://127.0.0.1:3766"

                html = request_text(f"{base}/?token={token}", token)
                self.assertIn('class="app-header"', html)
                self.assertIn("LoopPlane", html)
                self.assertIn("Live Dashboard", html)
                self.assertIn("loopplane_logo_dark.png", html)
                self.assertIn("loopplane_logo_light.png", html)
                self.assertIn("dashboard-refresh-button", html)
                self.assertIn("Auto-refresh every 30s.", html)
                server_payload = dashboard_script_payload(html)
                self.assertTrue(server_payload["initial_dashboard_load"])
                self.assertEqual(server_payload["read_models"], {})
                self.assertEqual(server_payload["jsonl_models"], {})
                js = request_text(f"{base}/static_dashboard.js", token)
                css = request_text(f"{base}/static_dashboard.css", token)
                logos = []
                for filename in ("loopplane_logo_dark.png", "loopplane_logo_light.png"):
                    logo_request = Request(f"{base}/{filename}", headers={"Authorization": f"Bearer {token}"})
                    with urlopen(logo_request, timeout=5) as logo_response:
                        self.assertEqual(logo_response.headers.get_content_type(), "image/png")
                        logos.append(logo_response.read())
                self.assertIn("mountNodeDetails", js)
                self.assertIn("REFRESH_INTERVAL_MS = 30000", js)
                self.assertTrue(all(logo.startswith(b"\x89PNG\r\n\x1a\n") for logo in logos))
                self.assertIn(".dashboard-shell", css)
                self.assertIn(".app-header", css)
                self.assertIn(".status-strip", css)
                self.assertIn(".graph-pipeline-scroll", css)
                self.assertIn("flex: 0 0 auto", css)
                self.assertIn("grid-template-columns: minmax(340px, 430px) repeat(3", css)
                self.assertIn('"vc runner approval metrics"', css)
                self.assertIn("--graph-row-body-max: 860px", css)
                self.assertIn("--graph-pipeline-max: 580px", css)
                self.assertIn("scroll-padding-inline: 0", css)
                self.assertIn("overflow-y: hidden", css)
                self.assertIn(".workflow-history-list", css)
                self.assertIn('data-workflow-history-mode="selected"', css)
                self.assertIn(".workflow-history-toggle", css)
                self.assertIn("graphNodeActiveBorder", css)
                self.assertIn(".panel-collapse-toggle", css)
                self.assertIn(".panel-body", css)
                self.assertIn("setWorkflowHistoryMode", js)
                self.assertIn("workflowHistoryShowAll", js)
                health = request_json(f"{base}/api/health", token)
                self.assertTrue(health["ok"], json.dumps(health, indent=2, sort_keys=True))
                workspace = request_json(f"{base}/api/workspace", token)
                self.assertEqual(workspace["workspace"]["current_workflow_id"], workflow_id)
                status = request_json(f"{base}/api/workflows/{workflow_id}/status", token)
                self.assertEqual(status["data"]["workflow_id"], workflow_id)
                protected_read_models = [paths.read_models_dir / filename for filename in READ_MODEL_FILES]
                diagnostics_before = file_hashes(project, protected_read_models)
                strict_diagnostics = request_json(f"{base}/api/workflows/{workflow_id}/read-model-diagnostics", token)
                diagnostics_after = file_hashes(project, protected_read_models)
                self.assertEqual(diagnostics_after, diagnostics_before)
                self.assertTrue(strict_diagnostics["ok"], json.dumps(strict_diagnostics, indent=2, sort_keys=True))
                self.assertTrue(strict_diagnostics["strict"])
                self.assertEqual(strict_diagnostics["checks"]["events"]["chain"]["status"], "pass")
                self.assertTrue(str(strict_diagnostics["checks"]["events"]["events_sha256"]).startswith("sha256:"))
                self.assertEqual(strict_diagnostics["checks"]["run_details"]["status"], "pass")
                controls = request_json(f"{base}/api/workflows/{workflow_id}/control-requests", token)
                self.assertEqual(controls["workflow_id"], workflow_id)
                dashboard_url = f"{base}/api/workflows/{workflow_id}/dashboard-data"
                status_code, dashboard_body, dashboard_headers = request_status_text(
                    dashboard_url,
                    token=token,
                    headers={"Accept": "application/json"},
                )
                self.assertEqual(status_code, 200)
                dashboard_payload = json.loads(dashboard_body)
                self.assertTrue(dashboard_payload["ok"], json.dumps(dashboard_payload, indent=2, sort_keys=True))
                self.assertTrue(dashboard_payload["plan_markdown"]["content"])
                self.assertTrue(dashboard_payload["read_models"]["workflow_graph.json"]["nodes"])
                self.assertIn("run_index.jsonl", dashboard_payload["jsonl_models"])
                self.assertNotIn("run_summaries.jsonl", dashboard_payload["jsonl_models"])
                self.assertEqual(dashboard_headers.get("X-LoopPlane-Dashboard-Result"), "full")
                etag = dashboard_headers.get("ETag")
                self.assertIsNotNone(etag)
                self.assertEqual(dashboard_payload["dashboard_etag"], etag)
                dashboard_diagnostics = dashboard_payload["dashboard_diagnostics"]
                self.assertEqual(dashboard_diagnostics["response_mode"], "full")
                self.assertGreaterEqual(dashboard_diagnostics["timings"]["etag_ms"], 0)
                self.assertGreaterEqual(dashboard_diagnostics["timings"]["payload_build_ms"], 0)
                self.assertGreaterEqual(dashboard_diagnostics["timings"]["total_ms"], 0)
                diagnostics = dashboard_payload["read_model_diagnostics"]["read_models"]
                self.assertTrue(diagnostics["cache_enabled"])
                self.assertIn(workflow_id, diagnostics["scope"])
                self.assertIn("cache_snapshot", diagnostics)
                unchanged_status, unchanged_body, unchanged_headers = request_status_text(
                    dashboard_url,
                    token=token,
                    headers={"Accept": "application/json", "If-None-Match": str(etag)},
                )
                self.assertEqual(unchanged_status, 304)
                self.assertEqual(unchanged_body, "")
                self.assertEqual(unchanged_headers.get("ETag"), etag)
                self.assertEqual(unchanged_headers.get("X-LoopPlane-Dashboard-Result"), "not_modified")
                self.assertIsNotNone(unchanged_headers.get("X-LoopPlane-Dashboard-Duration-Ms"))
                alias_status, alias_body, alias_headers = request_status_text(
                    f"{base}/api/dashboard?workflow={quote(workflow_id)}",
                    token=token,
                    headers={"Accept": "application/json", "If-None-Match": str(etag)},
                )
                self.assertEqual(alias_status, 304)
                self.assertEqual(alias_body, "")
                self.assertEqual(alias_headers.get("ETag"), etag)
                append_event(
                    paths,
                    workflow_id=workflow_id,
                    event_type="dashboard_etag_marker",
                    data={"source": "test_cli_dashboard_fixed_port_3766_serves_static_assets_and_core_api"},
                    snapshot_interval=None,
                )
                changed_status, changed_body, changed_headers = request_status_text(
                    dashboard_url,
                    token=token,
                    headers={"Accept": "application/json", "If-None-Match": str(etag)},
                )
                self.assertEqual(changed_status, 200)
                changed_payload = json.loads(changed_body)
                self.assertTrue(changed_payload["ok"], json.dumps(changed_payload, indent=2, sort_keys=True))
                self.assertNotEqual(changed_headers.get("ETag"), etag)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_cli_dashboard_port_starts_token_protected_server_and_writes_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            figure_path = project / "sales_analysis" / "revenue_trend.png"
            figure_path.parent.mkdir(parents=True, exist_ok=True)
            figure_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture-png")
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            enable_approvals(project)
            approval_id = "approval_dashboard_api_fixture"
            append_pending_approval(project, workflow_id, approval_id)
            plan_before = (project / "PLAN.md").read_bytes()
            state_before = (project / ".loopplane" / "runtime" / "state.json").read_bytes()
            events_before = (paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                self.assertTrue(startup["ok"], json.dumps(startup, indent=2, sort_keys=True))
                self.assertEqual(startup["status"], "serving")
                self.assertEqual(startup["server_mode"], True)
                self.assertEqual(startup["port"], port)
                self.assertEqual(startup["access_link_file"], (project / "LOOPPLANE_DASHBOARD.url").as_posix())
                token_path = project / str(startup["token_file"])
                self.assertTrue(token_path.is_file())
                token = token_path.read_text(encoding="utf-8").strip()
                access_link = project / str(startup["access_link_file"])
                self.assertTrue(access_link.is_file())
                access_link_text = access_link.read_text(encoding="utf-8")
                self.assertIn(f"http://127.0.0.1:{port}/?token=", access_link_text)
                self.assertIn(token, access_link_text)
                base = f"http://127.0.0.1:{port}"

                with self.assertRaises(HTTPError) as unauthorized:
                    request_text(f"{base}/api/workspace", "wrong-token")
                self.assertEqual(unauthorized.exception.code, 401)

                html = request_text(f"{base}/?token={token}", token)
                self.assertIn('class="app-header"', html)
                self.assertIn("LoopPlane", html)
                self.assertIn("Live Dashboard", html)
                self.assertIn("plan-view-toggle", html)
                self.assertIn("panel-collapse-toggle", html)
                self.assertIn('data-panel-key="runner"', html)
                self.assertIn('id="theme-toggle"', html)
                self.assertIn("dashboard-refresh-button", html)
                self.assertIn("Auto-refresh every 30s.", html)
                self.assertNotIn('<button id="dashboard-refresh-button" type="button" disabled>', html)
                self.assertIn("planning-control-form", html)
                self.assertIn("Run Planner", html)
                self.assertIn("Run Auditor", html)
                self.assertIn("Activate Plan", html)
                self.assertIn("execution-control-form", html)
                self.assertIn('data-control-action="start"', html)
                self.assertIn('data-control-action="pause"', html)
                self.assertIn('data-control-action="resume"', html)
                self.assertIn('data-control-action="stop"', html)
                self.assertIn("Inspector Chat / Change Request Console", html)
                self.assertIn("inspector-chat-form", html)
                self.assertIn("change-request-form", html)
                server_payload = dashboard_script_payload(html)
                self.assertTrue(server_payload["server_mode"])
                self.assertTrue(server_payload["initial_dashboard_load"])
                self.assertEqual(server_payload["read_models"], {})
                self.assertEqual(server_payload["jsonl_models"], {})
                self.assertIn("workflow_summaries", server_payload)
                self.assertNotIn("workflow_snapshots", server_payload)

                dashboard_payload = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertTrue(dashboard_payload["server_mode"])
                self.assertEqual(
                    path_content_records(dashboard_payload["node_details"]),
                    [],
                    "live dashboard node_details should lazy-load file content",
                )
                self.assertIn("run_index.jsonl", dashboard_payload["jsonl_models"])
                self.assertNotIn("run_summaries.jsonl", dashboard_payload["jsonl_models"])
                self.assertEqual(
                    path_content_records(dashboard_payload["jsonl_models"]["run_index.jsonl"]),
                    [],
                    "live dashboard run_index should not carry run detail content",
                )
                self.assertEqual(
                    path_content_records(dashboard_payload["read_models"]["plan_index.json"]),
                    [],
                    "live dashboard plan_index summaries should lazy-load markdown content",
                )
                self.assertEqual(
                    path_content_records(dashboard_payload["read_models"]["workflow_graph.json"]),
                    [],
                    "live dashboard workflow_graph summaries should lazy-load markdown content",
                )
                self.assertTrue(dashboard_payload["planning_controls"]["mutation_allowed"])
                self.assertEqual(
                    dashboard_payload["planning_controls"]["endpoints"],
                    {
                        "plan": f"/api/workflows/{workflow_id}/plan",
                        "audit": f"/api/workflows/{workflow_id}/audit",
                        "activate_plan": f"/api/workflows/{workflow_id}/activate-plan",
                    },
                )
                self.assertTrue(dashboard_payload["execution_controls"]["mutation_allowed"])
                self.assertEqual(
                    dashboard_payload["execution_controls"]["endpoints"],
                    {
                        "start": f"/api/workflows/{workflow_id}/control-requests",
                        "pause": f"/api/workflows/{workflow_id}/control-requests",
                        "resume": f"/api/workflows/{workflow_id}/control-requests",
                        "stop": f"/api/workflows/{workflow_id}/control-requests",
                    },
                )
                self.assertTrue(dashboard_payload["read_model_rebuild"]["mutation_allowed"])
                self.assertEqual(
                    dashboard_payload["read_model_rebuild"]["endpoint"],
                    f"/api/workflows/{workflow_id}/rebuild-read-models",
                )
                self.assertTrue(dashboard_payload["inspector_console"]["mutation_allowed"])
                self.assertTrue(dashboard_payload["inspector_console"]["chat_allowed"])
                self.assertTrue(dashboard_payload["inspector_console"]["change_request_allowed"])
                self.assertEqual(
                    dashboard_payload["inspector_console"]["endpoints"],
                    {
                        "chat": f"/api/workflows/{workflow_id}/chat",
                        "change_request": f"/api/workflows/{workflow_id}/change-requests",
                    },
                )
                workspace = request_json(f"{base}/api/workspace", token)
                self.assertEqual(workspace["workspace"]["current_workflow_id"], workflow_id)
                workspaces = request_json(f"{base}/api/workspaces", token)
                self.assertEqual(workspaces["workspaces"][0]["current_workflow_id"], workflow_id)
                workspace_workflows = request_json(f"{base}/api/workspace/workflows", token)
                self.assertEqual(workspace_workflows["workflows"][0]["workflow_id"], workflow_id)
                workspace_workflow = request_json(f"{base}/api/workspace/workflows/{workflow_id}", token)
                self.assertEqual(workspace_workflow["workflow_id"], workflow_id)
                status = request_json(f"{base}/api/workflows/{workflow_id}/status", token)
                self.assertEqual(status["data"]["workflow_id"], workflow_id)
                plan_index = request_json(f"{base}/api/workflows/{workflow_id}/plan-index", token)
                self.assertTrue(plan_index["data"]["phases"])
                graph = request_json(f"{base}/api/workflows/{workflow_id}/graph", token)
                self.assertTrue(graph["data"]["nodes"])
                run_id = next(node["run_id"] for node in graph["data"]["nodes"] if node.get("run_id"))
                run_detail = request_json(f"{base}/api/workflows/{workflow_id}/runs/{run_id}", token)
                self.assertEqual(run_detail["run_id"], run_id)
                self.assertEqual(run_detail["source"], "run_details")
                self.assertEqual(run_detail["node_detail"]["run_id"], run_id)
                api_sections = sections_by_key(run_detail["node_detail"])
                direct_sections = sections_by_key(run_detail["details"])
                self.assertTrue(REQUIRED_RUN_DETAIL_SECTIONS.issubset(api_sections), sorted(api_sections))
                self.assertEqual(set(api_sections), set(direct_sections))
                for section_key in REQUIRED_RUN_DETAIL_SECTIONS:
                    self.assertTrue(api_sections[section_key]["available"], section_key)
                self.assertNotIn("content", api_sections["prompt"])
                self.assertNotIn("content", api_sections["final_output"])
                self.assertNotIn("content", api_sections["human_summary"])
                self.assertEqual(api_sections["prompt"]["render_mode"], "markdown")
                self.assertEqual(api_sections["final_output"]["render_mode"], "markdown")
                self.assertEqual(api_sections["report"]["render_mode"], "markdown")
                self.assertEqual(api_sections["human_summary"]["render_mode"], "markdown")
                self.assertEqual(path_content_records(run_detail["node_detail"]), [])
                self.assertEqual(path_content_records(run_detail["details"]), [])
                self.assertEqual(path_content_records(run_detail["run"]), [])
                prompt_text = request_text(
                    f"{base}/api/workflows/{workflow_id}/files?path={quote(api_sections['prompt']['path'])}",
                    token,
                )
                self.assertIn("Worker Prompt", prompt_text)
                final_text = request_text(
                    f"{base}/api/workflows/{workflow_id}/files?path={quote(api_sections['final_output']['path'])}",
                    token,
                )
                self.assertIn("Worker Final Output", final_text)
                stdout_log = next(item for item in api_sections["logs"]["items"] if str(item.get("path", "")).endswith("stdout.log"))
                log_text = request_text(f"{base}/api/workflows/{workflow_id}/files?path={quote(stdout_log['path'])}", token)
                self.assertIn("[REDACTED]", log_text)
                self.assertNotIn("super-secret-token", log_text)
                log_tail = request_text(
                    f"{base}/api/workflows/{workflow_id}/files?path={quote(stdout_log['path'])}&tail=1&max_lines=2",
                    token,
                )
                self.assertIn("Showing the last 2 lines", log_tail)
                self.assertNotIn("\nok\n", log_tail)
                self.assertIn("[REDACTED]", log_tail)
                self.assertNotIn("super-secret-token", log_tail)
                figure_request = Request(
                    f"{base}/api/workflows/{workflow_id}/files?path={quote('sales_analysis/revenue_trend.png')}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urlopen(figure_request, timeout=5) as figure_response:
                    self.assertEqual(figure_response.headers.get_content_type(), "image/png")
                    self.assertEqual(figure_response.read(), b"\x89PNG\r\n\x1a\nfixture-png")
                self.assertEqual(api_sections["diff_summary"]["changed_files_count"], 1)
                self.assertEqual(api_sections["diff_summary"]["changed_files"][0]["path"], "src/app.py")
                self.assertEqual(api_sections["git_checkpoint"]["before"]["checkpoint_id"], "cp_before_run_fixture")
                self.assertEqual(api_sections["git_checkpoint"]["after"]["checkpoint_id"], "cp_after_run_fixture")
                self.assertNotIn("events", api_sections)
                api_detail_json = json.dumps(run_detail, sort_keys=True)
                self.assertNotIn("super-secret-token", api_detail_json)
                self.assertNotIn("abcdefghijklmnop", api_detail_json)
                self.assertNotIn(".git/config", api_detail_json)
                self.assertNotIn("refs/loopplane/checkpoints", api_detail_json)
                self.assertNotIn("repository_root", api_detail_json)
                approvals = request_json(f"{base}/api/workflows/{workflow_id}/approvals", token)
                self.assertEqual(approvals["workflow_id"], workflow_id)
                self.assertEqual(approvals["pending_count"], 1)

                init_request = request_json(
                    f"{base}/api/workflows/init",
                    token,
                    body={"brief": "Dashboard init should only create a request."},
                )
                self.assertEqual(init_request["status"], "pending")

                plan_request = request_json(
                    f"{base}/api/workflows/{workflow_id}/plan",
                    token,
                    body={"runner_id": "planner"},
                )
                self.assertEqual(plan_request["status"], "pending")
                audit_request = request_json(
                    f"{base}/api/workflows/{workflow_id}/audit",
                    token,
                    body={"runner_id": "auditor"},
                )
                self.assertEqual(audit_request["status"], "pending")
                activate_request = request_json(
                    f"{base}/api/workflows/{workflow_id}/activate-plan",
                    token,
                    body={"plan": "PLAN_DRAFT.md"},
                )
                self.assertEqual(activate_request["status"], "pending")
                refreshed_dashboard = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertEqual(refreshed_dashboard["planning_controls"]["pending_count"], 3)
                self.assertEqual(
                    [record["type"] for record in refreshed_dashboard["planning_controls"]["recent"][-3:]],
                    ["plan", "audit", "activate_plan"],
                )
                for control_type in ("start", "pause", "resume", "stop"):
                    control_request = request_json(
                        f"{base}/api/workflows/{workflow_id}/control-requests",
                        token,
                        body={"type": control_type, "reason": f"dashboard {control_type} smoke"},
                    )
                    self.assertEqual(control_request["status"], "pending")
                    self.assertEqual(control_request["request"]["type"], control_type)
                control_status = request_json(f"{base}/api/workflows/{workflow_id}/control-requests", token)
                self.assertEqual(control_status["pending_count"], 4)
                self.assertEqual(
                    [record["type"] for record in control_status["controls"][-4:]],
                    ["start", "pause", "resume", "stop"],
                )
                refreshed_controls = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertEqual(refreshed_controls["execution_controls"]["pending_count"], 4)
                self.assertEqual(
                    [record["type"] for record in refreshed_controls["execution_controls"]["recent"][-4:]],
                    ["start", "pause", "resume", "stop"],
                )
                self.assertEqual((project / "PLAN.md").read_bytes(), plan_before)
                self.assertEqual((project / ".loopplane" / "runtime" / "state.json").read_bytes(), state_before)
                self.assertEqual((paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes(), events_before)
                approval_response = request_json(
                    f"{base}/api/workflows/{workflow_id}/approvals/{approval_id}/respond",
                    token,
                    body={"decision": "approved", "notes": "dashboard api smoke"},
                )
                self.assertEqual(approval_response["status"], "approved")
                chat_response = request_json(
                    f"{base}/api/workflows/{workflow_id}/chat",
                    token,
                    body={"message": "What is the workflow status?"},
                )
                self.assertEqual(chat_response["status"], "answered")
                self.assertFalse(chat_response["response"]["read_only"])
                self.assertEqual(chat_response["response"]["access_policy"], "full_agent_access")
                self.assertTrue(chat_response["commands_executed"])
                change_request = request_json(
                    f"{base}/api/workflows/{workflow_id}/change-requests",
                    token,
                    body={"user_request": "Add a dashboard smoke follow-up."},
                )
                self.assertEqual(change_request["status"], "pending_review")
                refreshed_inspector = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                inspector_console = refreshed_inspector["inspector_console"]
                self.assertEqual(inspector_console["chat_count"], 1)
                self.assertEqual(inspector_console["chat_answered_count"], 1)
                self.assertEqual(inspector_console["recent_chat"][-1]["status"], "answered")
                self.assertEqual(inspector_console["recent_chat"][-1]["request_id"], chat_response["request"]["request_id"])
                self.assertEqual(inspector_console["change_request_count"], 1)
                self.assertEqual(inspector_console["pending_review_count"], 1)
                self.assertEqual(
                    inspector_console["recent_change_requests"][-1]["change_request_id"],
                    change_request["change_request_id"],
                )
                self.assertEqual((project / "PLAN.md").read_bytes(), plan_before)
                events_after_inspector = (paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes()
                self.assertNotEqual(events_after_inspector, events_before)
                self.assertIn(b"inspector_adapter_started", events_after_inspector)

                dashboard_requests = project / ".loopplane" / "requests" / "dashboard_requests.jsonl"
                control_requests = project / ".loopplane" / "runtime" / "control_requests.jsonl"
                change_requests = project / ".loopplane" / "requests" / "change_requests.jsonl"
                approval_responses = project / ".loopplane" / "runtime" / "human_approval_responses.jsonl"
                chat_requests = project / ".loopplane" / "requests" / "chat_requests.jsonl"
                chat_responses = project / ".loopplane" / "requests" / "chat_responses.jsonl"
                self.assertTrue(dashboard_requests.is_file())
                self.assertTrue(control_requests.is_file())
                self.assertTrue(change_requests.is_file())
                self.assertTrue(approval_responses.is_file())
                self.assertTrue(chat_requests.is_file())
                self.assertTrue(chat_responses.is_file())
                dashboard_records = read_jsonl(dashboard_requests)
                self.assertTrue(any(record.get("type") == "workflow_init" for record in dashboard_records))
                self.assertTrue(any(record.get("type") == "plan" for record in read_jsonl(dashboard_requests)))
                self.assertTrue(any(record.get("type") == "audit" for record in dashboard_records))
                self.assertTrue(any(record.get("type") == "activate_plan" for record in dashboard_records))
                self.assertEqual(
                    [record.get("type") for record in read_jsonl(control_requests)[-4:]],
                    ["start", "pause", "resume", "stop"],
                )
                self.assertTrue(any(record.get("decision") == "approved" for record in read_jsonl(approval_responses)))
                self.assertTrue(any(record.get("status") == "answered" for record in read_jsonl(chat_responses)))
                self.assertTrue(
                    any("dashboard smoke follow-up" in str(record.get("user_request") or "") for record in read_jsonl(change_requests))
                )
                self.assertEqual((project / "PLAN.md").read_bytes(), plan_before)
                self.assertIn(b"inspector_adapter_completed", (paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes())
                self.assertTrue((project / ".loopplane" / "runtime" / "dashboard_server.json").is_file())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_server_approval_panel_submits_responses_and_preserves_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            enable_approvals(project)
            append_pending_approval(
                project,
                workflow_id,
                "approval_server_approve",
                message="Approve server panel fixture.",
                evidence_path=(project / ".loopplane" / "results" / "T001" / "latest.json").as_posix(),
            )
            append_pending_approval(
                project,
                workflow_id,
                "approval_server_reject",
                message="Reject server panel fixture.",
            )
            approval_requests_path = project / ".loopplane" / "runtime" / "human_approval_requests.jsonl"
            plan_before = (project / "PLAN.md").read_bytes()
            state_before = (project / ".loopplane" / "runtime" / "state.json").read_bytes()
            events_before = (paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes()
            approval_requests_before = approval_requests_path.read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"
                approve_url = f"{base}/api/workflows/{workflow_id}/approvals/approval_server_approve/respond"
                reject_url = f"{base}/api/workflows/{workflow_id}/approvals/approval_server_reject/respond"

                html = request_text(f"{base}/?token={token}", token)
                self.assertIn("Approval Panel", html)
                server_payload = dashboard_script_payload(html)
                self.assertTrue(server_payload["initial_dashboard_load"])
                self.assertEqual(server_payload["approval_controls"]["status"], "loading")
                dashboard_data = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertEqual(dashboard_data["approval_controls"]["pending_count"], 2)
                self.assertTrue(dashboard_data["approval_controls"]["mutation_allowed"])
                self.assertEqual(
                    dashboard_data["approval_controls"]["pending"][0]["respond_endpoint"],
                    f"/api/workflows/{workflow_id}/approvals/approval_server_approve/respond",
                )

                status_code, body = request_json_status(approve_url, body={"decision": "approved"})
                self.assertEqual(status_code, 401)
                self.assertEqual(body["status"], "unauthorized")
                status_code, body = request_json_status(
                    approve_url,
                    token=token,
                    body={"decision": "approved"},
                    headers={"Origin": "http://evil.example"},
                )
                self.assertEqual(status_code, 403)
                self.assertEqual(body["status"], "forbidden")

                approvals_before = request_json(f"{base}/api/workflows/{workflow_id}/approvals", token)
                self.assertEqual(approvals_before["pending_count"], 2)
                self.assertIn(".loopplane/results/T001/latest.json", approvals_before["approvals"][0]["evidence_refs"])

                status_code, approved = request_json_status(
                    approve_url,
                    token=token,
                    body={
                        "decision": "approved",
                        "scope": "T001 one server smoke run",
                        "notes": "dashboard approval panel approve",
                    },
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertEqual(approved["status"], "approved")
                self.assertEqual(approved["response"]["scope"], "T001 one server smoke run")
                status_code, rejected = request_json_status(
                    reject_url,
                    token=token,
                    body={"decision": "rejected", "notes": "dashboard approval panel reject"},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertEqual(rejected["status"], "rejected")

                approvals_after = request_json(f"{base}/api/workflows/{workflow_id}/approvals", token)
                self.assertEqual(approvals_after["pending_count"], 0)
                statuses = {record["approval_id"]: record["status"] for record in approvals_after["approvals"]}
                self.assertEqual(statuses["approval_server_approve"], "approved")
                self.assertEqual(statuses["approval_server_reject"], "rejected")
                dashboard_data = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertEqual(dashboard_data["approval_controls"]["pending_count"], 0)
                self.assertEqual(dashboard_data["approval_controls"]["approved_count"], 1)
                self.assertEqual(dashboard_data["approval_controls"]["rejected_count"], 1)

                responses_path = project / ".loopplane" / "runtime" / "human_approval_responses.jsonl"
                responses = read_jsonl(responses_path)
                self.assertEqual([record["decision"] for record in responses[-2:]], ["approved", "rejected"])
                self.assertEqual((project / "PLAN.md").read_bytes(), plan_before)
                self.assertEqual((project / ".loopplane" / "runtime" / "state.json").read_bytes(), state_before)
                self.assertEqual((paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes(), events_before)
                self.assertEqual(approval_requests_path.read_bytes(), approval_requests_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_server_auto_rebuilds_stale_read_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, run_dir = prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            stale_event = append_event(
                paths,
                workflow_id=workflow_id,
                event_type="dashboard_rebuild_request_stale_marker",
                data={"task_id": "T001"},
                snapshot_interval=None,
            )
            read_model_paths = [paths.read_models_dir / filename for filename in READ_MODEL_FILES]
            authoritative_paths = [
                project / "PLAN.md",
                paths.runtime_dir / "state.json",
                paths.runtime_dir / "events" / "events_000001.jsonl",
                paths.results_dir / "T001" / "latest.json",
                run_dir / "validation.json",
            ]
            read_models_before = file_hashes(project, read_model_paths)
            authoritative_before = file_hashes(project, authoritative_paths)
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"
                endpoint = f"{base}/api/workflows/{workflow_id}/rebuild-read-models"

                html = request_text(f"{base}/?token={token}", token)
                payload = dashboard_script_payload(html)
                self.assertTrue(payload["initial_dashboard_load"])
                self.assertEqual(payload["read_models"], {})
                self.assertEqual(payload["jsonl_models"], {})
                self.assertIn("workflow_summaries", payload)
                self.assertNotIn("workflow_snapshots", payload)
                self.assertEqual(payload["read_model_freshness"]["status"], "loading")
                self.assertEqual(payload["read_model_rebuild"]["endpoint"], f"/api/workflows/{workflow_id}/rebuild-read-models")

                dashboard_data = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertEqual(dashboard_data["read_model_freshness"]["status"], "current")
                self.assertEqual(dashboard_data["read_model_freshness"]["event_log"]["source_event_id"], stale_event["event_id"])
                self.assertEqual(dashboard_data["read_model_freshness"]["event_log"]["freshness_mode"], "event_manifest")
                self.assertTrue(dashboard_data["rebuild_read_models"]["ok"])
                self.assertEqual(dashboard_data["rebuild_read_models"]["status"], "rebuilt")
                self.assertTrue(dashboard_data["read_model_rebuild"]["mutation_allowed"])
                self.assertTrue(dashboard_data["read_model_rebuild"]["request_allowed"])
                self.assertFalse(dashboard_data["read_model_rebuild"]["rebuild_in_progress"])
                self.assertEqual(
                    dashboard_data["read_model_rebuild"]["endpoint"],
                    f"/api/workflows/{workflow_id}/rebuild-read-models",
                )
                self.assertNotEqual(file_hashes(project, read_model_paths), read_models_before)
                self.assertEqual(file_hashes(project, authoritative_paths), authoritative_before)

                status_code, unauthorized = request_json_status(endpoint, body={"reason": "missing token"})
                self.assertEqual(status_code, 401)
                self.assertEqual(unauthorized["status"], "unauthorized")
                control_requests_path = paths.runtime_dir / "control_requests.jsonl"
                if control_requests_path.exists():
                    self.assertFalse(any(record.get("type") == "rebuild_read_models" for record in read_jsonl(control_requests_path)))
                self.assertFalse((paths.requests_dir / "dashboard_requests.jsonl").exists())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_runner_config_api_requires_trusted_local_and_records_request_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            write_runner_boundary_fixture(project, command="codex")
            set_dashboard_trusted_local(project, False)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            agent_config_path = project / ".loopplane" / "config" / "agent_runners.json"
            plan_before = (project / "PLAN.md").read_bytes()
            state_before = (project / ".loopplane" / "runtime" / "state.json").read_bytes()
            events_before = (paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes()
            config_before = agent_config_path.read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"
                runners_url = f"{base}/api/workflows/{workflow_id}/runners"
                request_url = f"{base}/api/workflows/{workflow_id}/runners/configuration-requests"

                runners = request_json(runners_url, token)
                self.assertEqual(runners["status"], "read_only")
                self.assertFalse(runners["runner_configuration"]["trusted_local_mode"])
                self.assertTrue(
                    all(
                        runner["command"] == "[hidden until trusted local mode]"
                        for runner in runners["runner_configuration"]["runners"]
                    )
                )

                status_code, body = request_json_status(
                    request_url,
                    token=token,
                    body={"runner_id": "planner", "role": "planner", "adapter": "codex_cli", "command": "codex"},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 403)
                self.assertEqual(body["status"], "trusted_local_required")
                self.assertFalse((project / ".loopplane" / "requests" / "dashboard_requests.jsonl").exists())

                set_dashboard_trusted_local(project, True)
                trusted_html = request_text(f"{base}/?token={token}", token)
                trusted_shell = dashboard_script_payload(trusted_html)
                self.assertTrue(trusted_shell["initial_dashboard_load"])
                trusted_dashboard = request_json(f"{base}/api/workflows/{workflow_id}/dashboard-data", token)
                self.assertTrue(trusted_dashboard["runner_configuration"]["trusted_local_mode"])
                self.assertTrue(
                    any(
                        runner["runner_id"] == "worker"
                        and runner["timeout_seconds"] == 21600
                        for runner in trusted_dashboard["runner_configuration"]["runners"]
                    )
                )
                trusted_runners = request_json(runners_url, token)
                self.assertEqual(trusted_runners["status"], "trusted_local")
                self.assertTrue(trusted_runners["runner_configuration"]["trusted_local_mode"])
                self.assertEqual(trusted_runners["runner_configuration"]["runners"][0]["runner_id"], "worker")
                self.assertTrue(
                    any(
                        runner["runner_id"] == "planner"
                        and runner["role"] == "planner"
                        and runner["adapter"] == "codex_cli"
                        and runner["command"] == "codex"
                        and runner["prompt_delivery_mode"] == "file_argument"
                        and runner["timeout_seconds"] == 21600
                        and runner["doctor"]["status"] == "not_run"
                        for runner in trusted_runners["runner_configuration"]["runners"]
                    )
                )
                self.assertNotIn("runner-secret-123456", json.dumps(trusted_runners, sort_keys=True))
                self.assertNotIn("env", json.dumps(trusted_runners["runner_configuration"], sort_keys=True))

                status_code, body = request_json_status(
                    request_url,
                    token=token,
                    body={
                        "runner_id": "planner",
                        "role": "planner",
                        "adapter": "codex_cli",
                        "command": "",
                        "model": "gpt-5.1-codex",
                        "reasoning_effort": "high",
                        "prompt_delivery_mode": "stdin",
                        "timeout_seconds": 1234,
                    },
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertEqual(body["status"], "pending")
                self.assertTrue(body["runner_configuration_applied"])
                self.assertEqual(body["request"]["type"], "runner_configuration")
                requested = body["request"]["payload"]["requested_configuration"]
                self.assertEqual(requested["runner_id"], "planner")
                self.assertNotIn("command", requested)
                self.assertEqual(requested["adapter_options"]["model"], "gpt-5.1-codex")
                self.assertEqual(requested["adapter_options"]["reasoning_effort"], "high")
                self.assertEqual(requested["prompt_delivery_mode"], "stdin")
                self.assertEqual(requested["timeout_seconds"], 1234)
                self.assertIsNone(body["request"]["payload"]["safe_command_path"])
                self.assertEqual(body["applied_configuration"]["model"], "gpt-5.1-codex")
                self.assertEqual(body["applied_configuration"]["reasoning_effort"], "high")

                status_code, unsafe = request_json_status(
                    request_url,
                    token=token,
                    body={"runner_id": "planner", "command": "/tmp/codex"},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 400)
                self.assertEqual(unsafe["status"], "local_path_command_rejected")

                dashboard_records = read_jsonl(project / ".loopplane" / "requests" / "dashboard_requests.jsonl")
                self.assertEqual(1, sum(1 for record in dashboard_records if record.get("type") == "runner_configuration"))
                self.assertEqual(agent_config_path.read_bytes(), config_before)
                local_override = json.loads(
                    (project / ".loopplane" / "config" / "local" / "agent_runners.local.json").read_text(encoding="utf-8")
                )
                planner_override = local_override["runners"]["planner"]
                self.assertEqual(planner_override["timeout_seconds"], 1234)
                self.assertEqual(planner_override["adapter_options"]["model"], "gpt-5.1-codex")
                self.assertEqual(planner_override["adapter_options"]["reasoning_effort"], "high")
                self.assertEqual((project / "PLAN.md").read_bytes(), plan_before)
                self.assertEqual((project / ".loopplane" / "runtime" / "state.json").read_bytes(), state_before)
                self.assertEqual((paths.runtime_dir / "events" / "events_000001.jsonl").read_bytes(), events_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_chat_api_rejects_non_inspector_runner_without_writing_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                status_code, body = request_json_status(
                    f"{base}/api/workflows/{workflow_id}/chat",
                    token=token,
                    body={"runner_id": "worker", "message": "Show workflow status."},
                    headers={"Origin": base},
                )

                self.assertEqual(status_code, 400)
                self.assertEqual(body["status"], "waiting_config")
                self.assertIn("expected 'inspector'", "\n".join(body["errors"]))
                self.assertFalse(read_jsonl(project / ".loopplane" / "requests" / "chat_requests.jsonl"))
                self.assertFalse(read_jsonl(project / ".loopplane" / "requests" / "chat_responses.jsonl"))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_server_workspace_selector_data_switches_without_current_pointer_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current["workflow_id"]
            archived_workflow_id = "wf_server_selector_archived"
            write_canonical_read_models(project, archived_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_server_selector",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "server selector current",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": archived_workflow_id,
                        "name": "server selector archived",
                        "status": "archived_view",
                        "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                        "read_only": True,
                        "archived": True,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_server_selector",
                        "current_workflow_id": current_workflow_id,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                html = request_text(f"{base}/?token={token}", token)
                self.assertIn("Workflow History Selector", html)
                self.assertIn("server selector archived", html)
                dashboard_data = request_json(f"{base}/api/workflows/{archived_workflow_id}/dashboard-data", token)
                self.assertTrue(dashboard_data["ok"], json.dumps(dashboard_data, indent=2, sort_keys=True))
                self.assertEqual(dashboard_data["workflow_id"], archived_workflow_id)
                self.assertEqual(
                    dashboard_data["read_models"]["workflow_status.json"]["status"],
                    "archived_view",
                )
                self.assertFalse(dashboard_data["planning_controls"]["mutation_allowed"])
                self.assertIn("archived", dashboard_data["planning_controls"]["mutation_blockers"])
                self.assertFalse(dashboard_data["execution_controls"]["mutation_allowed"])
                self.assertIn("archived", dashboard_data["execution_controls"]["mutation_blockers"])
                self.assertFalse(dashboard_data["approval_controls"]["mutation_allowed"])
                self.assertIn("archived", dashboard_data["approval_controls"]["mutation_blockers"])
                self.assertFalse(dashboard_data["read_model_rebuild"]["mutation_allowed"])
                self.assertIn("archived", dashboard_data["read_model_rebuild"]["mutation_blockers"])
                self.assertEqual(dashboard_data["workspace"]["current_workflow_id"], current_workflow_id)
                self.assertEqual(dashboard_data["workspace"]["selected_workflow_id"], archived_workflow_id)
                self.assertEqual(current_path.read_bytes(), current_before)

                status_code, body = request_json_status(
                    f"{base}/api/workflows/{archived_workflow_id}/control-requests",
                    token=token,
                    body={"type": "pause"},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 409)
                self.assertEqual(body["status"], "read_only_workflow")
                self.assertEqual(current_path.read_bytes(), current_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_api_resolves_canonical_v16_workflow_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            current_workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current_workflow["workflow_id"]
            canonical_workflow_id = "wf_canonical_dashboard_api"
            canonical_root = write_canonical_read_models(project, canonical_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "current flat workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": canonical_workflow_id,
                        "name": "canonical archived attempt",
                        "status": "review",
                        "workflow_root": f".loopplane/workflows/{canonical_workflow_id}",
                        "read_only": False,
                        "archived": False,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (project / ".loopplane" / "current_workflow.json").write_text(
                json.dumps(
                    {"schema_version": "1.6", "current_workflow_id": current_workflow_id},
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            flat_request_path = project / ".loopplane" / "requests" / "dashboard_requests.jsonl"
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                workflow_record = request_json(f"{base}/api/workspace/workflows/{canonical_workflow_id}", token)
                self.assertEqual(workflow_record["workflow_id"], canonical_workflow_id)
                status = request_json(f"{base}/api/workflows/{canonical_workflow_id}/status", token)
                self.assertEqual(status["data"]["workflow_id"], canonical_workflow_id)
                self.assertEqual(status["data"]["status"], "archived_view")
                plan_index = request_json(f"{base}/api/workflows/{canonical_workflow_id}/plan-index", token)
                self.assertEqual(plan_index["data"]["phases"][0]["tasks"][0]["task_id"], "C001")
                run = request_json(f"{base}/api/workflows/{canonical_workflow_id}/runs/run_canonical", token)
                self.assertEqual(run["run"]["summary"], "Canonical run summary.")

                plan_request = request_json(
                    f"{base}/api/workflows/{canonical_workflow_id}/plan",
                    token,
                    body={"runner_id": "planner"},
                )
                self.assertEqual(plan_request["status"], "pending")
                canonical_request_path = canonical_root / "requests" / "dashboard_requests.jsonl"
                self.assertTrue(canonical_request_path.is_file())
                self.assertTrue(any(record.get("type") == "plan" for record in read_jsonl(canonical_request_path)))
                self.assertFalse(flat_request_path.exists())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_workspace_scoped_api_detail_and_create_use_project_local_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            fake_home = Path(tmp) / "fake_home"
            prepare_dashboard_project(project)
            current_workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            current_workflow_id = current_workflow["workflow_id"]
            archived_workflow_id = "wf_20260611_abcd1234"
            write_canonical_read_models(project, archived_workflow_id)
            local_workspace_id = "ws_workspace_api_local"
            registry = {
                "schema_version": "1.6",
                "workspace_id": local_workspace_id,
                "generated_at": "2026-06-11T00:00:00Z",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "local completed workflow",
                        "status": "completed",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                    },
                    {
                        "workflow_id": archived_workflow_id,
                        "name": "local archived workflow",
                        "status": "archived",
                        "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                        "read_only": True,
                        "archived": True,
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            workspace_path = project / ".loopplane" / "workspace.json"
            workspace_metadata = json.loads(workspace_path.read_text(encoding="utf-8"))
            workspace_metadata["workspace_id"] = local_workspace_id
            workspace_metadata["current_workflow_id"] = current_workflow_id
            workspace_path.write_text(json.dumps(workspace_metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": local_workspace_id,
                        "current_workflow_id": current_workflow_id,
                        "selection_reason": "test_fixture",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            fake_registry = fake_home / "registry" / "workspaces.json"
            fake_registry.parent.mkdir(parents=True, exist_ok=True)
            fake_registry.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspaces": [
                            {
                                "workspace_id": "ws_global_spoof",
                                "project_root": "/tmp/not-the-project",
                                "current_workflow_id": "wf_20260611_deadbeef",
                            }
                        ],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            fake_global_before = fake_registry.read_bytes()
            current_before = current_path.read_bytes()

            port = free_local_port()
            env = os.environ.copy()
            env["LOOPPLANE_HOME"] = fake_home.as_posix()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                unauthorized_status, unauthorized = request_json_status(
                    f"{base}/api/workspace/workflows",
                    body={"brief": "Unauthorized workflow create."},
                    headers={"Origin": base},
                )
                self.assertEqual(unauthorized_status, 401)
                self.assertEqual(unauthorized["status"], "unauthorized")
                self.assertEqual(current_path.read_bytes(), current_before)

                forbidden_status, forbidden = request_json_status(
                    f"{base}/api/workspace/workflows",
                    token=token,
                    body={"brief": "Wrong-origin workflow create."},
                    headers={"Origin": "http://127.0.0.1:1"},
                )
                self.assertEqual(forbidden_status, 403)
                self.assertEqual(forbidden["status"], "forbidden")
                self.assertEqual(current_path.read_bytes(), current_before)

                workspace = request_json(f"{base}/api/workspace", token)
                self.assertEqual(workspace["workspace"]["workspace_id"], local_workspace_id)
                self.assertNotIn("ws_global_spoof", json.dumps(workspace, sort_keys=True))

                workflow_list = request_json(f"{base}/api/workspace/workflows", token)
                self.assertEqual(
                    [record["workflow_id"] for record in workflow_list["workflows"]],
                    [current_workflow_id, archived_workflow_id],
                )
                archived_detail = request_json(f"{base}/api/workspace/workflows/{archived_workflow_id}", token)
                self.assertEqual(archived_detail["workflow_id"], archived_workflow_id)
                self.assertEqual(
                    archived_detail["read_models"]["workflow_status.json"]["status"],
                    "archived_view",
                )
                self.assertFalse(archived_detail["planning_controls"]["mutation_allowed"])
                self.assertIn("read_only", archived_detail["planning_controls"]["mutation_blockers"])
                self.assertEqual(archived_detail["workspace"]["current_workflow_id"], current_workflow_id)
                self.assertEqual(archived_detail["workspace"]["selected_workflow_id"], archived_workflow_id)
                self.assertEqual(current_path.read_bytes(), current_before)

                missing_status, missing = request_json_status(
                    f"{base}/api/workspace/workflows/wf_20260611_ffffffff",
                    token=token,
                )
                self.assertEqual(missing_status, 404)
                self.assertEqual(missing["status"], "workflow_not_found")
                self.assertEqual(current_path.read_bytes(), current_before)

                readonly_status, readonly = request_json_status(
                    f"{base}/api/workflows/{archived_workflow_id}/control-requests",
                    token=token,
                    body={"type": "pause"},
                    headers={"Origin": base},
                )
                self.assertEqual(readonly_status, 409)
                self.assertEqual(readonly["status"], "read_only_workflow")
                self.assertEqual(current_path.read_bytes(), current_before)

                created_status, created = request_json_status(
                    f"{base}/api/workspace/workflows",
                    token=token,
                    body={"brief": "Create a new workspace-scoped dashboard workflow."},
                    headers={"Origin": base},
                )
                self.assertEqual(created_status, 201, json.dumps(created, indent=2, sort_keys=True))
                self.assertEqual(created["status"], "created")
                created_workflow_id = created["workflow_id"]
                self.assertNotEqual(created_workflow_id, current_workflow_id)
                self.assertEqual(
                    json.loads(current_path.read_text(encoding="utf-8"))["current_workflow_id"],
                    created_workflow_id,
                )
                updated_registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
                self.assertIn(created_workflow_id, [record["workflow_id"] for record in updated_registry["workflows"]])
                self.assertEqual(fake_registry.read_bytes(), fake_global_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_preserves_older_workflow_history_after_current_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            older_workflow_id = "wf_dashboard_history_old"
            current_workflow_id = "wf_dashboard_history_new"
            write_canonical_read_models(project, older_workflow_id)
            write_canonical_read_models(project, current_workflow_id)
            registry = {
                "schema_version": "1.6",
                "workspace_id": "ws_dashboard_history_preserve",
                "workflows": [
                    {
                        "workflow_id": older_workflow_id,
                        "name": "older archived dashboard history",
                        "status": "archived",
                        "workflow_root": f".loopplane/workflows/{older_workflow_id}",
                        "read_only": True,
                        "archived": True,
                        "summary": {
                            "one_line": "Older dashboard history should remain viewable.",
                            "tasks_total": 1,
                            "tasks_completed": 0,
                            "tasks_blocked": 0,
                        },
                    },
                    {
                        "workflow_id": current_workflow_id,
                        "name": "new current dashboard history",
                        "status": "active",
                        "workflow_root": f".loopplane/workflows/{current_workflow_id}",
                        "read_only": False,
                        "archived": False,
                        "summary": {
                            "one_line": "New current workflow after a workflow change.",
                            "tasks_total": 1,
                            "tasks_completed": 0,
                            "tasks_blocked": 0,
                        },
                    },
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            current_path = project / ".loopplane" / "current_workflow.json"
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": "ws_dashboard_history_preserve",
                        "current_workflow_id": current_workflow_id,
                        "selection_reason": "workflow_created",
                        "updated_at": "2026-06-11T00:00:00Z",
                        "updated_by": "test",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            current_before = current_path.read_bytes()
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                workspace_workflows = request_json(f"{base}/api/workspace/workflows", token)
                self.assertEqual(workspace_workflows["current_workflow_id"], current_workflow_id)
                self.assertEqual(
                    [record["workflow_id"] for record in workspace_workflows["workflows"]],
                    [older_workflow_id, current_workflow_id],
                )

                older_payload = request_json(f"{base}/api/workflows/{older_workflow_id}/dashboard-data", token)
                self.assertTrue(older_payload["ok"], json.dumps(older_payload, indent=2, sort_keys=True))
                self.assertEqual(older_payload["workflow_id"], older_workflow_id)
                self.assertEqual(older_payload["workspace"]["current_workflow_id"], current_workflow_id)
                self.assertEqual(older_payload["workspace"]["selected_workflow_id"], older_workflow_id)
                self.assertEqual(
                    older_payload["read_models"]["workflow_status.json"]["workflow_id"],
                    older_workflow_id,
                )
                self.assertEqual(
                    older_payload["read_models"]["workflow_status.json"]["status"],
                    "archived_view",
                )
                self.assertFalse(older_payload["execution_controls"]["mutation_allowed"])
                self.assertIn("archived", older_payload["execution_controls"]["mutation_blockers"])

                current_payload = request_json(f"{base}/api/workflows/{current_workflow_id}/dashboard-data", token)
                self.assertTrue(current_payload["ok"], json.dumps(current_payload, indent=2, sort_keys=True))
                self.assertEqual(current_payload["workflow_id"], current_workflow_id)
                self.assertEqual(current_payload["workspace"]["selected_workflow_id"], current_workflow_id)
                api_selected = request_json(f"{base}/api/dashboard?workflow={older_workflow_id}", token)
                self.assertTrue(api_selected["ok"], json.dumps(api_selected, indent=2, sort_keys=True))
                self.assertEqual(api_selected["workflow_id"], older_workflow_id)
                self.assertEqual(api_selected["workspace"]["current_workflow_id"], current_workflow_id)
                self.assertEqual(api_selected["workspace"]["selected_workflow_id"], older_workflow_id)
                self.assertEqual(current_path.read_bytes(), current_before)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_security_rejects_spoofed_origin_redacts_records_and_limits_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            paths, _run_dir = prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            secret = "loopplane-secret-123456"
            status_path = paths.read_models_dir / "workflow_status.json"
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            status_payload["diagnostics"] = {
                "api_key": secret,
                "message": f"API_KEY={secret} PASSWORD={secret}",
            }
            status_path.write_text(json.dumps(status_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token_path = project / str(startup["token_file"])
                self.assertTrue(token_path.is_file())
                if sys.platform != "win32":
                    self.assertEqual(stat.S_IMODE(token_path.stat().st_mode) & 0o077, 0)
                token = token_path.read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"
                plan_url = f"{base}/api/workflows/{workflow_id}/plan"

                status_code, body = request_json_status(plan_url, body={"runner_id": "planner"})
                self.assertEqual(status_code, 401)
                self.assertEqual(body["status"], "unauthorized")

                status_code, body = request_json_status(
                    plan_url,
                    token=token,
                    body={"runner_id": "planner"},
                    headers={"Origin": "http://evil.example"},
                )
                self.assertEqual(status_code, 403)
                self.assertEqual(body["status"], "forbidden")

                status_code, body = request_json_status(
                    plan_url,
                    token=token,
                    body={"runner_id": "planner"},
                    headers={"Origin": f"http://evil.local:{port}", "Host": f"evil.local:{port}"},
                )
                self.assertEqual(status_code, 403)
                self.assertEqual(body["status"], "forbidden")

                sensitive_body = {
                    "runner_id": "planner",
                    "note": f"API_KEY={secret} PASSWORD={secret}",
                    "nested": {"access_token": secret},
                }
                status_code, body = request_json_status(
                    plan_url,
                    token=token,
                    body=sensitive_body,
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertNotIn(secret, json.dumps(body, sort_keys=True))

                status_code, body = request_json_status(
                    f"{base}/api/workflows/{workflow_id}/control-requests",
                    token=token,
                    body={"type": "pause", "reason": f"PASSWORD={secret}"},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertNotIn(secret, json.dumps(body, sort_keys=True))

                status_code, body = request_json_status(
                    f"{base}/api/workflows/{workflow_id}/change-requests",
                    token=token,
                    body={"user_request": f"Add follow-up with TOKEN={secret}."},
                    headers={"Origin": base},
                )
                self.assertEqual(status_code, 202)
                self.assertNotIn(secret, json.dumps(body, sort_keys=True))

                dashboard_requests = (project / ".loopplane" / "requests" / "dashboard_requests.jsonl").read_text(
                    encoding="utf-8"
                )
                control_requests = (project / ".loopplane" / "runtime" / "control_requests.jsonl").read_text(
                    encoding="utf-8"
                )
                change_requests = (project / ".loopplane" / "requests" / "change_requests.jsonl").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn(secret, dashboard_requests)
                self.assertNotIn(secret, control_requests)
                self.assertNotIn(secret, change_requests)
                self.assertIn("[REDACTED]", dashboard_requests)
                self.assertIn("[REDACTED]", control_requests)
                self.assertIn("[REDACTED]", change_requests)

                status_code, read_model = request_json_status(f"{base}/read_models/workflow_status.json", token=token)
                self.assertEqual(status_code, 200)
                self.assertNotIn(secret, json.dumps(read_model, sort_keys=True))
                self.assertEqual(read_model["diagnostics"]["api_key"], "[REDACTED]")

                status_code, traversal = request_json_status(
                    f"{base}/read_models/..%2Fruntime%2Fstate.json",
                    token=token,
                )
                self.assertEqual(status_code, 404)
                self.assertEqual(traversal["status"], "not_found")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_security_rejects_mutating_requests_for_read_only_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            prepare_dashboard_project(project)
            workflow = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))
            workflow_id = workflow["workflow_id"]
            enable_approvals(project)
            append_pending_approval(project, workflow_id, "approval_read_only_reject")
            registry = {
                "schema_version": "1.6",
                "workflows": [
                    {
                        "workflow_id": workflow_id,
                        "name": "read-only current workflow",
                        "status": "read_only_imported",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "read_only": True,
                        "archived": False,
                    }
                ],
            }
            (project / ".loopplane" / "workflow_registry.json").write_text(
                json.dumps(registry, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            port = free_local_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(LoopPlane),
                    "dashboard",
                    "--project",
                    str(project),
                    "--port",
                    str(port),
                    "--json",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                startup = read_server_startup(process)
                token = (project / str(startup["token_file"])).read_text(encoding="utf-8").strip()
                base = f"http://127.0.0.1:{port}"

                for endpoint, body_payload in (
                    ("plan", {"runner_id": "planner"}),
                    ("audit", {"runner_id": "auditor"}),
                    ("activate-plan", {"plan": "PLAN_DRAFT.md"}),
                    ("control-requests", {"type": "stop"}),
                    ("rebuild-read-models", {"reason": "read-only rebuild request"}),
                    ("approvals/approval_read_only_reject/respond", {"decision": "approved"}),
                    ("chat", {"message": "What is blocked?"}),
                    ("change-requests", {"user_request": "Add a read-only workflow follow-up."}),
                ):
                    status_code, body = request_json_status(
                        f"{base}/api/workflows/{workflow_id}/{endpoint}",
                        token=token,
                        body=body_payload,
                        headers={"Origin": base},
                    )

                    self.assertEqual(status_code, 409)
                    self.assertEqual(body["status"], "read_only_workflow")
                self.assertFalse((project / ".loopplane" / "requests" / "dashboard_requests.jsonl").exists())
                self.assertFalse(read_jsonl(project / ".loopplane" / "runtime" / "control_requests.jsonl"))
                self.assertFalse(read_jsonl(project / ".loopplane" / "runtime" / "human_approval_responses.jsonl"))
                self.assertFalse(read_jsonl(project / ".loopplane" / "requests" / "chat_requests.jsonl"))
                self.assertFalse(read_jsonl(project / ".loopplane" / "requests" / "chat_responses.jsonl"))
                self.assertFalse(read_jsonl(project / ".loopplane" / "requests" / "change_requests.jsonl"))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def test_dashboard_renders_pending_approval_attention_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Dashboard approval attention.")
            set_approval_enabled(project, True)
            write_approval_required_plan(project)
            scheduler = run_scheduler(project, max_ticks=1)
            self.assertEqual(scheduler["selected_action"]["action"], "wait_approval")
            rebuild = rebuild_read_models(project)
            self.assertTrue(rebuild["ok"], json.dumps(rebuild, indent=2, sort_keys=True))

            result = render_static_dashboard(project, output_dir=project / "dashboard_approval")

            self.assertTrue(result["ok"], json.dumps(result, indent=2, sort_keys=True))
            html = (project / result["index_file"]).read_text(encoding="utf-8")
            self.assertIn("Requires Attention", html)
            self.assertIn("Approve execution of T001", html)
            self.assertIn("loopplane approvals --project", html)
            self.assertIn("loopplane approve approval_", html)
            self.assertIn("loopplane reject approval_", html)


if __name__ == "__main__":
    unittest.main()
