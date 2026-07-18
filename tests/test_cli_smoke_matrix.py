from __future__ import annotations

import json
import os
import select
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.init_workflow import init_project
from runtime.skill_package import (
    DEFERRED_CLI_COMMANDS,
    REQUIRED_NON_STUB_CLI_COMMANDS,
    check_required_command_handlers,
)
from tests.test_dashboard import prepare_dashboard_project


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
SPEC_PATH = REPO_ROOT / "LoopPlane.md"
CLI_ADAPTER_FIXTURE_BIN = REPO_ROOT / "tests" / "fixtures" / "cli_adapters" / "bin"

STUB_OR_RESERVED_PATTERNS = (
    "not implemented in the current standalone runtime",
    "not_implemented",
    "reserved for the future",
    "reserved until implemented",
    "stub command",
)

PROJECT_AWARE_COMMANDS = {
    "configure-agent",
    "doctor-agent",
    "write-brief",
    "plan",
    "audit-plan",
    "activate-plan",
    "start",
    "run",
    "preview",
    "tick",
    "pause",
    "resume",
    "stop",
    "attach",
    "status",
    "health",
    "logs",
    "summarize",
    "rebuild-read-models",
    "migrate",
    "export",
    "dashboard",
    "ask",
    "change-request",
    "approvals",
    "approve",
    "reject",
}

JSON_AWARE_COMMANDS = PROJECT_AWARE_COMMANDS | {"skill", "vc", "workspace", "workflow"}

DEFERRED_SPEC_LINE_PREFIXES = (
    "loopplane workspace ",
    "loopplane workflow ",
    "loopplane export ",
    "loopplane import ",
)


@dataclass
class SmokeContext:
    root: Path
    project: Path
    plan_project: Path
    dashboard_project: Path
    workflow_create_project: Path
    workflow_fork_project: Path
    import_target: Path
    read_only_import_target: Path
    install_target: Path
    update_target: Path
    init_project_target: Path
    init_cwd_target: Path
    pack_output: Path
    loopplane_home: Path


@dataclass
class SmokeRun:
    spec_line: str
    classification: str
    command_path: tuple[str, ...]
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    deferred_rationale: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "spec_line": self.spec_line,
            "classification": self.classification,
            "command_path": " ".join(self.command_path),
            "smoke_command": " ".join(["loopplane", *(shlex.quote(arg) for arg in self.args)]),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "deferred_rationale": self.deferred_rationale,
        }


def loopplane_md_25_command_lines() -> list[str]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    start = text.index("## 25. CLI Specification")
    end = text.index("## 26. MVP Scope", start)
    section = text[start:end]
    return _loopplane_command_lines_from_markdown_fences(section)


def loopplane_md_2_2_cli_flow_command_lines() -> list[str]:
    text = SPEC_PATH.read_text(encoding="utf-8")
    start = text.index("### 2.2 CLI flow")
    end = text.index("### 2.3 Dashboard flow", start)
    section = text[start:end]
    return _loopplane_command_lines_from_markdown_fences(section)


def _loopplane_command_lines_from_markdown_fences(section: str) -> list[str]:
    in_fence = False
    commands: list[str] = []
    for raw_line in section.splitlines():
        if raw_line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        stripped = raw_line.strip()
        if stripped.startswith("loopplane "):
            commands.append(normalize_spec_line(stripped))
    return commands


def normalize_spec_line(line: str) -> str:
    return " ".join(line.split("#", 1)[0].strip().split())


def command_tokens(spec_line: str) -> list[str]:
    return shlex.split(normalize_spec_line(spec_line))


def materialize_shell_variables(tokens: list[str], variables: dict[str, str]) -> list[str]:
    materialized: list[str] = []
    for token in tokens:
        if token.startswith("$") and token[1:] in variables:
            materialized.append(variables[token[1:]])
        else:
            materialized.append(token)
    return materialized


def command_path_for_spec(spec_line: str) -> tuple[str, ...]:
    tokens = command_tokens(spec_line)
    if not tokens or tokens[0] != "loopplane":
        raise AssertionError(f"unexpected LoopPlane command line: {spec_line}")
    args = tokens[1:]
    if not args:
        return ()
    if args[0] == "change-request":
        return ("change-request", "submit")
    if args[0] in {"skill", "vc", "workspace", "workflow"}:
        if len(args) >= 2 and not args[1].startswith("-") and not args[1].startswith("<"):
            return (args[0], args[1])
        return (args[0],)
    if args[0] == "dashboard" and len(args) >= 2 and args[1] == "list":
        return ("dashboard", "list")
    return (args[0],)


def deferred_rationale_for_spec(spec_line: str) -> str | None:
    normalized = normalize_spec_line(spec_line)
    if not normalized.startswith(DEFERRED_SPEC_LINE_PREFIXES):
        return None
    tokens = command_tokens(normalized)[1:]
    deferred: dict[tuple[str, ...], str] = {}
    for entry in DEFERRED_CLI_COMMANDS:
        command = tuple(str(part) for part in entry["command"])
        profiles = entry.get("profiles")
        if isinstance(profiles, tuple):
            profile = _profile_value(tokens)
            if profile not in profiles:
                continue
        flags = entry.get("flags")
        if isinstance(flags, tuple) and not all(str(flag) in tokens for flag in flags):
            continue
        deferred[command] = str(entry["rationale"])
    exact = tuple(tokens[:2]) if tokens and tokens[0] in {"dashboard", "vc", "workspace", "workflow"} else tuple(tokens[:1])
    if exact in deferred:
        return deferred[exact]
    prefix = tuple(tokens[:1])
    if prefix in deferred:
        return deferred[prefix]
    required = {tuple(command) for command in REQUIRED_NON_STUB_CLI_COMMANDS}
    if exact in required or prefix in required:
        return None
    raise AssertionError(f"deferred spec command lacks DEFERRED_CLI_COMMANDS metadata: {spec_line}")


def _profile_value(tokens: list[str]) -> str | None:
    for index, token in enumerate(tokens):
        if token == "--profile" and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith("--profile="):
            return token.split("=", 1)[1]
    return None


def build_smoke_context(root: Path) -> SmokeContext:
    project = root / "project"
    plan_project = root / "plan-project"
    dashboard_project = root / "dashboard-project"
    workflow_create_project = root / "workflow-create-project"
    workflow_fork_project = root / "workflow-fork-project"
    import_target = root / "stateful-import-target"
    read_only_import_target = root / "read-only-import-target"
    install_target = root / "install-target"
    update_target = root / "update-target"
    init_project_target = root / "init-project-target"
    init_cwd_target = root / "init-cwd-target"
    loopplane_home = root / "loopplane-home"
    env = {**os.environ, "LOOPPLANE_HOME": str(loopplane_home)}
    env["PATH"] = f"{CLI_ADAPTER_FIXTURE_BIN}{os.pathsep}{env.get('PATH', '')}"
    init_cwd_target.mkdir(parents=True)
    init_project(project, "LoopPlane CLI smoke fixture.")
    init_project(plan_project, "LoopPlane planner CLI smoke fixture.")
    init_project(workflow_create_project, "LoopPlane workflow create smoke fixture.")
    init_project(workflow_fork_project, "LoopPlane workflow fork smoke fixture.")
    prepare_dashboard_project(dashboard_project)
    configure_noop_runner(plan_project, "planner", "planner", env=env)
    configure_noop_runner(plan_project, "auditor", "auditor", env=env)
    install = run_cli(["skill", "install", "--target", str(update_target), "--json"], env=env)
    if install.returncode != 0:
        raise AssertionError(install.stderr + install.stdout)
    return SmokeContext(
        root=root,
        project=project,
        plan_project=plan_project,
        dashboard_project=dashboard_project,
        workflow_create_project=workflow_create_project,
        workflow_fork_project=workflow_fork_project,
        import_target=import_target,
        read_only_import_target=read_only_import_target,
        install_target=install_target,
        update_target=update_target,
        init_project_target=init_project_target,
        init_cwd_target=init_cwd_target,
        pack_output=root / "artifacts" / "loopplane-smoke.zip",
        loopplane_home=loopplane_home,
    )


def configure_noop_runner(project: Path, runner_id: str, role: str, *, env: dict[str, str] | None = None) -> None:
    result = run_cli(
        [
            "configure-agent",
            "--project",
            str(project),
            "--runner",
            runner_id,
            "--role",
            role,
            "--adapter",
            "noop",
            "--command",
            "noop",
            "--json",
        ],
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr + result.stdout)


def run_cli(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LoopPlane), *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_dashboard_server_smoke(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        [sys.executable, str(LoopPlane), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_line = ""
    stderr = ""
    try:
        assert process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                stdout_line = process.stdout.readline()
                break
            if process.poll() is not None:
                break
        if not stdout_line and process.stderr is not None:
            stderr = process.stderr.read()
        if stdout_line:
            payload = json.loads(stdout_line)
            if not payload.get("ok"):
                return subprocess.CompletedProcess([sys.executable, str(LoopPlane), *args], 1, stdout_line, stderr)
            return subprocess.CompletedProcess([sys.executable, str(LoopPlane), *args], 0, stdout_line, stderr)
        return subprocess.CompletedProcess([sys.executable, str(LoopPlane), *args], process.poll() or 1, "", stderr)
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


def wait_for_detached_supervisor_to_settle(project: Path, env: dict[str, str]) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        result = run_cli(["status", "--json"], cwd=project, env=env, timeout=10.0)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return
        supervisor = payload.get("supervisor")
        if not isinstance(supervisor, dict) or supervisor.get("liveness") != "alive":
            break
        time.sleep(0.2)
    _wait_for_supervisor_process_exit(project)


def _wait_for_supervisor_process_exit(project: Path) -> None:
    pattern = f"runtime.detached supervisor --project {project}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            return
        if result.returncode != 0:
            return
        time.sleep(0.2)


def materialize_smoke_args(spec_line: str, context: SmokeContext) -> tuple[list[str], Path | None, bool]:
    normalized = normalize_spec_line(spec_line)
    args = command_tokens(normalized)[1:]
    cwd: Path | None = None
    server_mode = False

    if normalized == "loopplane skill doctor":
        return [*args, "--json"], cwd, server_mode
    if normalized == "loopplane skill install --target <project>":
        return ["skill", "install", "--target", str(context.install_target), "--json"], cwd, server_mode
    if normalized == "loopplane skill update --target <project>":
        return ["skill", "update", "--target", str(context.update_target), "--json"], cwd, server_mode
    if normalized == "loopplane skill pack":
        return ["skill", "pack", "--output", str(context.pack_output), "--json"], cwd, server_mode

    if normalized == "loopplane configure-agent":
        return ["configure-agent", "--project", str(context.project), "--runner", "worker", "--json"], cwd, server_mode
    if normalized in {
        "loopplane configure-agent --role worker --adapter codex_cli --command codex",
        'loopplane configure-agent --role worker --adapter codex_cli --command "$CODEX_BIN"',
    }:
        return [
            "configure-agent",
            "--project",
            str(context.project),
            "--role",
            "worker",
            "--adapter",
            "codex_cli",
            "--command",
            sys.executable,
            "--json",
        ], cwd, server_mode
    if normalized in {
        "loopplane configure-agent --role worker --adapter claude_code_cli --command claude",
        'loopplane configure-agent --role worker --adapter claude_code_cli --command "$CLAUDE_BIN"',
    }:
        return [
            "configure-agent",
            "--project",
            str(context.project),
            "--role",
            "worker",
            "--adapter",
            "claude_code_cli",
            "--command",
            sys.executable,
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane doctor-agent":
        return ["doctor-agent", "--project", str(context.project), "--runner", "worker", "--json"], cwd, server_mode

    if normalized == "loopplane init --project <path>":
        return ["init", "--project", str(context.init_project_target), "--brief", "CLI smoke init project."], cwd, server_mode
    if normalized == 'loopplane init --brief "..."':
        return ["init", "--brief", "CLI smoke init cwd."], context.init_cwd_target, server_mode

    if normalized == "loopplane write-brief":
        return [
            "write-brief",
            "--project",
            str(context.project),
            "--text",
            "Updated CLI smoke brief.",
            "--force",
            "--json",
        ], cwd, server_mode
    if args[0] in {"plan", "audit-plan", "activate-plan"}:
        return [args[0], "--project", str(context.plan_project), "--json"], cwd, server_mode

    if normalized == "loopplane start --detach":
        return ["start", "--detach", "--project", str(context.project), "--json"], cwd, server_mode
    if normalized == "loopplane dashboard":
        return ["dashboard", "--project", str(context.dashboard_project), "--json"], cwd, server_mode
    if normalized == "loopplane dashboard --port 3766":
        return [
            "dashboard",
            "--project",
            str(context.dashboard_project),
            "--port",
            str(free_local_port()),
            "--json",
        ], cwd, True
    if normalized == "loopplane dashboard --port auto":
        return ["dashboard", "--project", str(context.dashboard_project), "--port", "auto", "--json"], cwd, True
    if normalized == "loopplane dashboard list":
        return ["dashboard", "list", "--project", str(context.dashboard_project), "--json"], cwd, server_mode
    if normalized == "loopplane workspace current":
        return ["workspace", "current", "--project", str(context.project), "--json"], cwd, server_mode
    if normalized == "loopplane workspace doctor":
        return ["workspace", "doctor", "--project", str(context.project), "--json"], cwd, server_mode
    if normalized == "loopplane workflow list":
        return ["workflow", "list", "--project", str(context.project), "--json"], cwd, server_mode
    if normalized == "loopplane workflow current":
        return ["workflow", "current", "--project", str(context.project), "--json"], cwd, server_mode
    if normalized == "loopplane workflow show <workflow_id>":
        workflow_config = json.loads(
            (context.project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
        )
        return [
            "workflow",
            "show",
            str(workflow_config["workflow_id"]),
            "--project",
            str(context.project),
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane workflow switch <workflow_id>":
        workflow_config = json.loads(
            (context.project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
        )
        return [
            "workflow",
            "switch",
            str(workflow_config["workflow_id"]),
            "--project",
            str(context.project),
            "--json",
        ], cwd, server_mode
    if normalized == 'loopplane workflow create --brief "..."':
        return [
            "workflow",
            "create",
            "--brief",
            "CLI smoke workflow create.",
            "--project",
            str(context.workflow_create_project),
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane workflow archive <workflow_id>":
        workflow_config = json.loads(
            (context.workflow_create_project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
        )
        return [
            "workflow",
            "archive",
            str(workflow_config["workflow_id"]),
            "--project",
            str(context.workflow_create_project),
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane workflow restore <workflow_id>":
        workflow_config = json.loads(
            (context.workflow_create_project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
        )
        return [
            "workflow",
            "restore",
            str(workflow_config["workflow_id"]),
            "--project",
            str(context.workflow_create_project),
            "--json",
        ], cwd, server_mode
    if normalized == 'loopplane workflow fork <workflow_id> --name "new attempt"':
        workflow_config = json.loads(
            (context.workflow_fork_project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8")
        )
        return [
            "workflow",
            "fork",
            str(workflow_config["workflow_id"]),
            "--name",
            "CLI smoke workflow fork.",
            "--project",
            str(context.workflow_fork_project),
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane import loopplane_stateful.tar.zst --target <project>":
        return [
            "import",
            str(context.root / "loopplane_stateful.tar.zst"),
            "--target",
            str(context.import_target),
            "--json",
        ], cwd, server_mode
    if normalized == "loopplane import loopplane_archive.tar.zst --target <project> --read-only":
        return [
            "import",
            str(context.root / "loopplane_archive.tar.zst"),
            "--target",
            str(context.read_only_import_target),
            "--read-only",
            "--json",
        ], cwd, server_mode

    replacements = {
        "<project>": str(context.project),
        "<path>": str(context.root / "path-placeholder"),
        "<directory>": str(context.root),
        "<workspace_id>": "ws_cli_smoke_missing",
        "<workflow_id>": "wf_cli_smoke_missing",
        "<approval_id>": "approval_cli_smoke_missing",
        "loopplane_source.tar.zst": str(context.root / "loopplane_source.tar.zst"),
        "loopplane_stateful.tar.zst": str(context.root / "loopplane_stateful.tar.zst"),
        "loopplane_archive.tar.zst": str(context.root / "loopplane_archive.tar.zst"),
        "loopplane_git_refs.bundle": str(context.root / "loopplane_git_refs.bundle"),
    }
    args = [replacements.get(arg, arg) for arg in args]
    if args[0] in PROJECT_AWARE_COMMANDS and "--project" not in args:
        args.extend(["--project", str(context.project)])
    if args[0] == "vc" and "--project" not in args:
        args.extend(["--project", str(context.project)])
    if args[0] in JSON_AWARE_COMMANDS and "--json" not in args:
        args.append("--json")
    return args, cwd, server_mode


def run_smoke_matrix(context: SmokeContext) -> list[SmokeRun]:
    runs: list[SmokeRun] = []
    env = os.environ.copy()
    env["LOOPPLANE_HOME"] = str(context.loopplane_home)
    env["PATH"] = f"{CLI_ADAPTER_FIXTURE_BIN}{os.pathsep}{env.get('PATH', '')}"
    for spec_line in loopplane_md_25_command_lines():
        args, cwd, server_mode = materialize_smoke_args(spec_line, context)
        completed = run_dashboard_server_smoke(args, env=env) if server_mode else run_cli(args, cwd=cwd, env=env)
        rationale = deferred_rationale_for_spec(spec_line)
        classification = "deferred" if rationale is not None else "required"
        runs.append(
            SmokeRun(
                spec_line=spec_line,
                classification=classification,
                command_path=command_path_for_spec(spec_line),
                args=args,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                deferred_rationale=rationale,
            )
        )
    return runs


def write_optional_artifacts(runs: list[SmokeRun], *, output_dir: str | None) -> None:
    if not output_dir:
        return
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    records = [run.as_record() for run in runs]
    (root / "command_matrix.json").write_text(
        json.dumps(
            {
                "schema_version": "loopplane-cli-smoke-matrix-1",
                "spec": "LoopPlane.md 25",
                "command_count": len(records),
                "commands": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "smoke_summary.json").write_text(
        json.dumps(
            {
                "schema_version": "loopplane-cli-smoke-summary-1",
                "command_count": len(records),
                "required_count": sum(1 for run in runs if run.classification == "required"),
                "deferred_count": sum(1 for run in runs if run.classification == "deferred"),
                "required_parser_failures": [
                    run.spec_line
                    for run in runs
                    if run.classification == "required" and parser_failed(run)
                ],
                "required_stub_outputs": [
                    run.spec_line
                    for run in runs
                    if run.classification == "required" and contains_stub_or_reserved_text(run)
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def parser_failed(run: SmokeRun) -> bool:
    return run.returncode == 2 and "usage: loopplane" in run.stderr and "error:" in run.stderr


def contains_stub_or_reserved_text(run: SmokeRun) -> bool:
    output = f"{run.stdout}\n{run.stderr}".lower()
    return any(pattern in output for pattern in STUB_OR_RESERVED_PATTERNS)


class CliSmokeMatrixTest(unittest.TestCase):
    def test_loopplane_md_2_2_cli_flow_executes_without_stub_outputs(self) -> None:
        commands = loopplane_md_2_2_cli_flow_command_lines()

        self.assertEqual(
            commands,
            [
                "loopplane skill install --target .",
                'loopplane write-brief --text "Add tests, fix failing behavior, run a smoke benchmark, and produce a final report." --force',
                "loopplane configure-agent --role worker --adapter codex_cli --command codex",
                "loopplane configure-agent --role planner --adapter codex_cli --command codex",
                "loopplane configure-agent --role auditor --adapter codex_cli --command codex",
                "loopplane doctor-agent --runner worker",
                "loopplane doctor-agent --runner planner",
                "loopplane doctor-agent --runner auditor",
                "loopplane plan",
                "loopplane audit-plan",
                "loopplane activate-plan",
                "loopplane start --detach",
                "loopplane dashboard",
            ],
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            env = os.environ.copy()
            env["PATH"] = f"{CLI_ADAPTER_FIXTURE_BIN}{os.pathsep}{env.get('PATH', '')}"
            runs: list[SmokeRun] = []
            try:
                for command in commands:
                    args = command_tokens(command)[1:]
                    completed = run_cli(args, cwd=project, env=env, timeout=30.0)
                    run = SmokeRun(
                        spec_line=command,
                        classification="required",
                        command_path=command_path_for_spec(command),
                        args=args,
                        returncode=completed.returncode,
                        stdout=completed.stdout,
                        stderr=completed.stderr,
                    )
                    runs.append(run)
                    with self.subTest(command=command):
                        self.assertEqual(completed.returncode, 0, run.as_record())
                        self.assertFalse(contains_stub_or_reserved_text(run), run.as_record())
            finally:
                if (project / ".loopplane").exists():
                    run_cli(["stop", "--json"], cwd=project, env=env, timeout=10.0)
                    wait_for_detached_supervisor_to_settle(project, env)

            self.assertTrue((project / ".loopplane" / "dashboard_static" / "index.html").is_file())
            self.assertEqual(len(runs), len(commands))

    def test_loopplane_md_25_command_lines_match_required_or_deferred_metadata(self) -> None:
        spec_lines = loopplane_md_25_command_lines()

        self.assertEqual(len(spec_lines), 69)
        self.assertEqual(len(spec_lines), len(set(spec_lines)))

        required = {tuple(command) for command in REQUIRED_NON_STUB_CLI_COMMANDS}
        deferred = []
        missing = []
        for spec_line in spec_lines:
            rationale = deferred_rationale_for_spec(spec_line)
            path = command_path_for_spec(spec_line)
            if rationale is not None:
                deferred.append((spec_line, rationale))
                continue
            if path not in required:
                missing.append((spec_line, " ".join(path)))

        self.assertFalse(missing, missing)
        for spec_line, rationale in deferred:
            self.assertIn("tracked by R", rationale, spec_line)

        handler_check = check_required_command_handlers(REPO_ROOT)
        self.assertEqual(handler_check["status"], "pass", handler_check)

    def test_loopplane_md_25_commands_have_explicit_cli_smoke_expectations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = build_smoke_context(Path(tmp))
            runs = run_smoke_matrix(context)
            write_optional_artifacts(runs, output_dir=os.environ.get("LOOPPLANE_CLI_SMOKE_OUTPUT_DIR"))

        self.assertEqual(len(runs), 69)
        required_runs = [run for run in runs if run.classification == "required"]
        deferred_runs = [run for run in runs if run.classification == "deferred"]
        self.assertTrue(required_runs)

        for run in required_runs:
            with self.subTest(command=run.spec_line):
                self.assertFalse(parser_failed(run), run.as_record())
                self.assertFalse(contains_stub_or_reserved_text(run), run.as_record())

        for run in deferred_runs:
            with self.subTest(command=run.spec_line):
                self.assertIsNotNone(run.deferred_rationale)
                self.assertIn("tracked by R", run.deferred_rationale or "")
                self.assertNotEqual(run.returncode, 0, run.as_record())


if __name__ == "__main__":
    unittest.main()
