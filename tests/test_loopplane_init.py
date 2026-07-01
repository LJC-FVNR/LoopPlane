from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from runtime.adapters.policy import (
    VERSION_CONTROL_MANAGER_ONLY_OPERATIONS,
    WORKER_DENIED_GIT_OPERATIONS,
    WORKER_GIT_READ_ONLY_OPERATIONS,
)
from runtime.exit_codes import EXIT_SECURITY_POLICY_VIOLATION
from runtime.init_workflow import InitConflictError, init_project
from runtime.schema_validation import validate_project_schemas
from runtime.self_expansion import DEFAULT_SELF_EXPANSION_POLICY
from runtime.version_control import GitCommandResult


REPO_ROOT = Path(__file__).resolve().parents[1]
LoopPlane = REPO_ROOT / "scripts" / "loopplane"
SCHEMA_VERSION = "1.5"
WORKSPACE_SCHEMA_VERSION = "1.6"
UTC_TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
WORKFLOW_ID_PATTERN = r"^wf_\d{8}_[0-9a-f]{8}$"
WORKSPACE_ID_PATTERN = r"^ws_[0-9A-Za-z][0-9A-Za-z_-]{7,63}$"
WORKFLOW_HISTORY_STATUSES = (
    "draft",
    "ready",
    "active",
    "running",
    "paused",
    "stopped",
    "objective_unresolved",
    "completed",
    "failed",
    "archived",
    "read_only_imported",
    "forked",
    "superseded",
)
CONFIG_FILES = (
    "workflow.json",
    "agent_runners.json",
    "dashboard.json",
    "security.json",
    "version_control.json",
    "schema_version.json",
)
WORKFLOW_PATH_FIELDS = (
    "brief_file",
    "plan_file",
    "shared_context_file",
    "results_dir",
    "runtime_dir",
    "read_models_dir",
    "requests_dir",
    "planning_dir",
    "version_control_config_file",
)
PROJECT_BRIEF_SECTIONS = (
    "# Project Brief",
    "## User Request",
    "## Goals",
    "## Available Resources",
    "## Constraints",
    "## Expected Deliverables",
    "## Success Signals",
    "## Non-goals",
    "## Assumptions",
    "## Open Questions",
)
SHARED_CONTEXT_SECTIONS = (
    "# Shared Context",
    "## Objective",
    "## Authority",
    "## Untrusted Input Rule",
    "## Worker Project Write Rules",
    "## Worker Workflow Output Rules",
    "## Completion Rules",
)
UNRESOLVED_PLACEHOLDER_MARKERS = (
    "{{",
    "}}",
    "<Original user request",
    "<Goal",
    "<Existing repository",
    "<Time, budget",
    "<Files, reports",
    "<Observable conditions",
    "<Explicitly out-of-scope",
    "<Assumptions made",
    "<Questions that truly block",
)
CONFIG_SCHEMAS = {
    "workflow.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version",
            "workflow_id",
            "created_at",
            "project_root",
            *WORKFLOW_PATH_FIELDS,
            "default_worker_runner",
            "planning",
            "execution",
        ],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "workflow_id": {"type": "string", "pattern": WORKFLOW_ID_PATTERN},
            "created_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
            "project_root": {"const": "."},
            **{field: {"type": "string", "minLength": 1} for field in WORKFLOW_PATH_FIELDS},
            "default_worker_runner": {"const": "worker"},
            "planning": {
                "type": "object",
                "required": [
                    "enabled",
                    "planner_runner",
                    "auditor_runner",
                    "max_planner_iterations",
                    "auditor_required",
                ],
            },
            "execution": {
                "type": "object",
                "required": ["max_concurrent_workers", "continue_on_fail", "recovery_before_new_work"],
            },
        },
    },
    "agent_runners.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_version", "default_runner", "runners"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "default_runner": {"const": "worker"},
            "runners": {
                "type": "object",
                "required": ["worker", "worker_fallback", "planner", "auditor", "validator", "change_request_planner", "summary", "final_reviewer", "inspector"],
            },
        },
    },
    "dashboard.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version",
            "enabled",
            "host",
            "port",
            "read_models_dir",
            "allow_chat",
            "chat_runner",
            "allow_change_requests",
            "allow_start_stop",
            "refresh_interval_ms",
            "preferred_port",
            "port_range",
            "server_state_file",
        ],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "host": {"const": "127.0.0.1"},
            "port": {"oneOf": [{"type": "integer", "minimum": 1, "maximum": 65535}, {"const": "auto"}]},
            "preferred_port": {"type": "integer", "minimum": 1, "maximum": 65535},
            "port_range": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
            "server_state_file": {"type": "string", "minLength": 1},
        },
    },
    "security.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["schema_version", "dashboard", "redaction", "approval", "git_command_policy", "file_access"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "dashboard": {"type": "object", "required": ["bind_host", "require_token", "token_file"]},
            "redaction": {"type": "object", "required": ["enabled", "redact_env_vars", "redact_patterns"]},
            "approval": {
                "type": "object",
                "required": ["enabled", "default_action_when_disabled"],
                "properties": {
                    "default_action_when_disabled": {"const": "auto_authorize"},
                },
            },
            "git_command_policy": {
                "type": "object",
                "required": [
                    "enforce_worker_boundaries",
                    "worker_allowed_read_only_operations",
                    "worker_denied_write_operations",
                    "version_control_manager_only_operations",
                    "adapter_enforcement",
                ],
            },
            "file_access": {"type": "object", "required": ["allowlist", "denylist"]},
        },
    },
    "version_control.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version",
            "enabled",
            "provider",
            "default_on",
            "user_configuration_required",
            "auto_init_if_missing",
            "repository_mode",
            "checkpoint_backend",
            "refs_namespace",
            "no_remote_push",
            "do_not_switch_user_branch",
            "do_not_modify_user_index",
            "checkpoint_policy",
            "commit_policy",
            "path_policy",
            "rollback_policy",
        ],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "provider": {"const": "git"},
            "repository_mode": {"const": "existing_or_local_init"},
            "checkpoint_backend": {"const": "managed_refs"},
            "refs_namespace": {"const": "refs/loopplane/{{workflow_id}}"},
        },
    },
    "schema_version.json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": [
            "schema_version",
            "created_with",
            "last_migrated_at",
            "required_runtime_version",
            "files",
        ],
        "properties": {
            "schema_version": {"enum": [SCHEMA_VERSION, WORKSPACE_SCHEMA_VERSION]},
            "created_with": {"const": "loopplane 1.5.0"},
            "last_migrated_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
            "required_runtime_version": {"const": ">=1.5.0"},
            "files": {
                "type": "object",
                "required": [*CONFIG_FILES],
                "additionalProperties": {"enum": [SCHEMA_VERSION, WORKSPACE_SCHEMA_VERSION]},
            },
            "compatibility": {
                "type": "object",
                "required": ["status", "legacy_schema_version", "reason", "legacy_schema_version_files"],
            },
        },
    },
}
WORKSPACE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "workspace_id",
        "project_root",
        "loopplane_dir",
        "repo_root",
        "created_at",
        "created_by_loopplane_version",
        "workspace_boundary",
        "allow_out_of_boundary_writes",
        "single_active_running_workflow",
    ],
    "properties": {
        "schema_version": {"const": WORKSPACE_SCHEMA_VERSION},
        "workspace_id": {"type": "string", "pattern": WORKSPACE_ID_PATTERN},
        "project_root": {"const": "."},
        "loopplane_dir": {"const": ".loopplane"},
        "repo_root": {"type": "string", "minLength": 1},
        "created_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
        "created_by_loopplane_version": {"type": "string", "minLength": 1},
        "workspace_boundary": {"const": "project_root"},
        "allow_out_of_boundary_writes": {"type": "boolean"},
        "single_active_running_workflow": {"type": "boolean"},
    },
}
WORKFLOW_REGISTRY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["schema_version", "workspace_id", "generated_at", "workflows"],
    "properties": {
        "schema_version": {"const": WORKSPACE_SCHEMA_VERSION},
        "workspace_id": {"type": "string", "pattern": WORKSPACE_ID_PATTERN},
        "generated_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
        "workflows": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": [
                    "workflow_id",
                    "name",
                    "status",
                    "workflow_root",
                    "created_at",
                    "last_seen_at",
                    "plan_file",
                    "read_models_dir",
                    "read_only",
                    "archived",
                    "summary",
                ],
                "properties": {
                    "workflow_id": {"type": "string", "pattern": WORKFLOW_ID_PATTERN},
                    "name": {"type": "string", "minLength": 1},
                    "status": {"enum": WORKFLOW_HISTORY_STATUSES},
                    "workflow_root": {"type": "string", "minLength": 1},
                    "created_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
                    "last_seen_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
                    "plan_file": {"type": "string", "minLength": 1},
                    "read_models_dir": {"type": "string", "minLength": 1},
                    "runtime_dir": {"type": "string", "minLength": 1},
                    "requests_dir": {"type": "string", "minLength": 1},
                    "completion_marker": {"type": "string", "minLength": 1},
                    "read_only": {"type": "boolean"},
                    "archived": {"type": "boolean"},
                    "summary": {
                        "type": "object",
                        "required": ["one_line", "tasks_total", "tasks_completed", "tasks_blocked"],
                    },
                },
            },
        },
    },
}
CURRENT_WORKFLOW_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": [
        "schema_version",
        "workspace_id",
        "current_workflow_id",
        "selection_reason",
        "updated_at",
        "updated_by",
    ],
    "properties": {
        "schema_version": {"const": WORKSPACE_SCHEMA_VERSION},
        "workspace_id": {"type": "string", "pattern": WORKSPACE_ID_PATTERN},
        "current_workflow_id": {"type": "string", "pattern": WORKFLOW_ID_PATTERN},
        "selection_reason": {"type": "string", "minLength": 1},
        "updated_at": {"type": "string", "pattern": UTC_TIMESTAMP_PATTERN},
        "updated_by": {"type": "string", "minLength": 1},
    },
}


class FailingGitInitRunner:
    def git_path(self) -> str:
        return "/usr/bin/git"

    def run(self, project_root: Path, args: tuple[str, ...]) -> GitCommandResult:
        if args == ("--version",):
            return GitCommandResult(0, "git version fake\n", "")
        if args == ("rev-parse", "--is-inside-work-tree"):
            return GitCommandResult(128, "", "fatal: not a git repository")
        if args == ("init",):
            return GitCommandResult(128, "", "fatal: cannot initialize repository")
        return GitCommandResult(128, "", "fatal: not a git repository")


class LoopPlaneInitIntegrationTest(unittest.TestCase):
    def run_loopplane(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(LoopPlane), *args],
            cwd=cwd or REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def current_workflow_paths(self, project: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, Path]:
        workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
        registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
        current = json.loads((project / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8"))
        record = next(
            workflow
            for workflow in registry["workflows"]
            if workflow["workflow_id"] == current["current_workflow_id"]
        )
        root_value = record["workflow_root"].rstrip("/")
        return workspace, registry, current, root_value, project / root_value

    def test_init_creates_project_local_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            brief_text = "  Ship the smallest useful slice.  "

            result = self.run_loopplane("init", "--project", str(project), "--brief", brief_text)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Initialized LoopPlane workflow", result.stdout)
            self.assertTrue((project / ".loopplane" / "workspace.json").is_file())
            self.assertTrue((project / ".loopplane" / "workflow_registry.json").is_file())
            self.assertTrue((project / ".loopplane" / "current_workflow.json").is_file())
            self.assertTrue((project / ".git").is_dir())

            workspace, registry, current, root_value, workflow_root = self.current_workflow_paths(project)
            registry_record = next(
                record
                for record in registry["workflows"]
                if record["workflow_id"] == current["current_workflow_id"]
            )
            self.assertEqual(registry_record["name"], "Ship the smallest useful slice.")
            self.assertEqual(registry_record["summary"]["one_line"], "Ship the smallest useful slice.")
            self.assertTrue((workflow_root / "PROJECT_BRIEF.md").is_file())
            self.assertTrue((workflow_root / "PLAN.md").is_file())
            self.assertTrue((workflow_root / "SHARED_CONTEXT.md").is_file())
            self.assertFalse((project / "PLAN.md").exists())
            self.assertTrue((workflow_root / "config" / "workflow.json").is_file())
            self.assertTrue((project / ".gitignore").is_file())
            self.assertTrue((project / ".loopplane" / "prompts" / "git_tracking_init.md").is_file())
            self.assertTrue((workflow_root / "runtime" / "state.json").is_file())
            self.assertTrue((workflow_root / "runtime" / "events" / "events_000001.jsonl").is_file())
            self.assertTrue((workflow_root / "read_models" / "workflow_status.json").is_file())
            self.assertTrue((workflow_root / "requests" / "change_requests.jsonl").is_file())

            brief = (workflow_root / "PROJECT_BRIEF.md").read_text(encoding="utf-8")
            self.assertIn(brief_text, brief)

            workflow = json.loads((workflow_root / "config" / "workflow.json").read_text(encoding="utf-8"))
            self.assertEqual(root_value, f".loopplane/workflows/{workflow['workflow_id']}")

            self.assertEqual(workflow["workspace_root"], ".loopplane")
            self.assertEqual(workflow["workflow_root"], root_value)
            self.assertEqual(workflow["workflow_config_file"], f"{root_value}/config/workflow.json")
            self.assertEqual(workflow["brief_file"], f"{root_value}/PROJECT_BRIEF.md")
            self.assertEqual(workflow["plan_file"], f"{root_value}/PLAN.md")
            self.assertEqual(workflow["shared_context_file"], f"{root_value}/SHARED_CONTEXT.md")
            self.assertEqual(workflow["runtime_dir"], f"{root_value}/runtime")
            self.assertEqual(workflow["read_models_dir"], f"{root_value}/read_models")
            self.assertEqual(workflow["requests_dir"], f"{root_value}/requests")
            self.assertEqual(workflow["results_dir"], f"{root_value}/results")
            gitignore = (project / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("# BEGIN LoopPlane MANAGED IGNORE", gitignore)
            self.assertIn("# END LoopPlane MANAGED IGNORE", gitignore)
            self.assertIn(f"/{root_value}/runtime/events/", gitignore)
            self.assertIn(f"/{root_value}/read_models/", gitignore)
            self.assertIn(f"/{root_value}/results/**/artifacts/", gitignore)
            self.assertIn("*.safetensors", gitignore)
            git_prompt = (project / ".loopplane" / "prompts" / "git_tracking_init.md").read_text(encoding="utf-8")
            self.assertIn("Lightweight Git Tracking Initialization", git_prompt)
            self.assertIn("Update only the block", git_prompt)
            self.assertEqual(workspace["schema_version"], WORKSPACE_SCHEMA_VERSION)
            self.assertRegex(workspace["workspace_id"], WORKSPACE_ID_PATTERN)
            self.assertNotEqual(workspace["workspace_id"], workflow["workflow_id"])
            self.assertEqual(workspace["project_root"], ".")
            self.assertEqual(workspace["loopplane_dir"], ".loopplane")
            self.assertEqual(workspace["repo_root"], ".")
            self.assertEqual(workspace["workspace_boundary"], "project_root")
            self.assertFalse(workspace["allow_out_of_boundary_writes"])
            self.assertTrue(workspace["single_active_running_workflow"])
            self.assertEqual(registry["schema_version"], WORKSPACE_SCHEMA_VERSION)
            self.assertEqual(registry["workspace_id"], workspace["workspace_id"])
            self.assertEqual(len(registry["workflows"]), 1)
            registry_record = registry["workflows"][0]
            self.assertEqual(registry_record["workflow_id"], workflow["workflow_id"])
            self.assertNotEqual(registry_record["workflow_id"], workspace["workspace_id"])
            self.assertEqual(registry_record["status"], "draft")
            self.assertIn(registry_record["status"], WORKFLOW_HISTORY_STATUSES)
            self.assertEqual(registry_record["workflow_root"], root_value)
            self.assertEqual(registry_record["workflow_config_file"], f"{root_value}/config/workflow.json")
            self.assertEqual(registry_record["plan_file"], f"{root_value}/PLAN.md")
            self.assertEqual(registry_record["read_models_dir"], f"{root_value}/read_models")
            self.assertEqual(registry_record["runtime_dir"], f"{root_value}/runtime")
            self.assertEqual(registry_record["requests_dir"], f"{root_value}/requests")
            self.assertEqual(registry_record["completion_marker"], f"{root_value}/runtime/plan_loop_complete.json")
            self.assertFalse(registry_record["read_only"])
            self.assertFalse(registry_record["archived"])
            self.assertEqual(registry_record["summary"]["tasks_total"], 0)
            self.assertEqual(current["schema_version"], WORKSPACE_SCHEMA_VERSION)
            self.assertEqual(current["workspace_id"], workspace["workspace_id"])
            self.assertEqual(current["current_workflow_id"], workflow["workflow_id"])
            self.assertEqual(current["current_workflow_id"], registry_record["workflow_id"])
            self.assertEqual(current["selection_reason"], "initial_workflow")
            self.assertRegex(current["updated_at"], UTC_TIMESTAMP_PATTERN)
            self.assertTrue(current["updated_by"])

    def test_init_json_reports_initialized_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            result = self.run_loopplane("init", "--project", str(project), "--brief", "JSON init.", "--json")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"], json.dumps(payload, indent=2, sort_keys=True))
            self.assertEqual(payload["status"], "initialized")
            self.assertEqual(payload["project_root"], project.resolve().as_posix())
            self.assertTrue(payload["workflow_id"].startswith("wf_"))
            self.assertTrue(payload["workspace_id"].startswith("ws_"))
            self.assertIn(".loopplane/workspace.json", payload["created"])
            self.assertEqual(payload["errors"], [])

    def test_init_preserves_existing_gitignore_and_appends_loopplane_managed_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            user_gitignore = "# user rules\nlocal-output/\n"
            (project / ".gitignore").write_text(user_gitignore, encoding="utf-8")

            result = self.run_loopplane("init", "--project", str(project), "--brief", "Existing gitignore.")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            _workspace, _registry, _current, root_value, _workflow_root = self.current_workflow_paths(project)
            gitignore = (project / ".gitignore").read_text(encoding="utf-8")
            self.assertTrue(gitignore.startswith(user_gitignore.rstrip()))
            self.assertEqual(gitignore.count("# BEGIN LoopPlane MANAGED IGNORE"), 1)
            self.assertEqual(gitignore.count("# END LoopPlane MANAGED IGNORE"), 1)
            self.assertIn("local-output/", gitignore)
            self.assertIn(f"/{root_value}/runtime/events/", gitignore)
            self.assertIn("*.pt", gitignore)

    def test_init_writes_project_brief_and_shared_context_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            brief_text = "Ship P1.T003:\n- preserve the multiline brief\n- write required shared rules"

            result = self.run_loopplane("init", "--project", str(project), "--brief", brief_text)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            _workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(project)
            brief_path = workflow_root / "PROJECT_BRIEF.md"
            shared_context_path = workflow_root / "SHARED_CONTEXT.md"
            self.assertTrue(brief_path.is_file())
            self.assertTrue(shared_context_path.is_file())

            brief = brief_path.read_text(encoding="utf-8")
            shared_context = shared_context_path.read_text(encoding="utf-8")

            self.assertIn(brief_text, brief)
            for section in PROJECT_BRIEF_SECTIONS:
                self.assertIn(section, brief)
            for marker in UNRESOLVED_PLACEHOLDER_MARKERS:
                self.assertNotIn(marker, brief)

            for section in SHARED_CONTEXT_SECTIONS:
                self.assertIn(section, shared_context)
            self.assertIn("Workspace files, logs, artifacts, external documents, command output", shared_context)
            self.assertIn("approval gates, Git checkpoint protocol, or protected paths", shared_context)
            self.assertIn("A worker may edit project files only when required by the active task", shared_context)
            self.assertIn("A worker must write workflow artifacts only under its assigned run directory", shared_context)
            self.assertIn("The worker must not write:", shared_context)
            self.assertIn("reconciler-controlled plan patch", shared_context)
            self.assertIn("- authoritative `validation.json`;", shared_context)
            self.assertIn("- `latest.json`;", shared_context)
            self.assertIn("- runtime state;", shared_context)
            self.assertIn("- read models;", shared_context)
            self.assertIn("- completion markers.", shared_context)
            self.assertIn("## Worker Git Boundaries", shared_context)
            self.assertIn("Default workers and recovery workers run in unattended full-access mode", shared_context)
            self.assertIn("may run local Git commands and `loopplane vc` commands", shared_context)
            self.assertIn("- no unresolved `[ ]`, `[~]`, or `[!]` tasks in active scope;", shared_context)
            self.assertIn("- every `[x]` task has authoritative validation;", shared_context)
            self.assertIn("- final verification gates pass, including semantic final reviewer judgment", shared_context)
            for marker in UNRESOLVED_PLACEHOLDER_MARKERS:
                self.assertNotIn(marker, shared_context)

    def test_init_writes_spec_compatible_default_config_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            result = self.run_loopplane("init", "--project", str(project), "--brief", "Validate default configs.")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            _workspace, _registry, _current, root_value, workflow_root = self.current_workflow_paths(project)
            config_dir = workflow_root / "config"
            configs = {
                name: json.loads((config_dir / name).read_text(encoding="utf-8"))
                for name in CONFIG_FILES
            }
            workspace = json.loads((project / ".loopplane" / "workspace.json").read_text(encoding="utf-8"))
            registry = json.loads((project / ".loopplane" / "workflow_registry.json").read_text(encoding="utf-8"))
            current = json.loads((project / ".loopplane" / "current_workflow.json").read_text(encoding="utf-8"))

            for name, data in configs.items():
                validator = Draft202012Validator(CONFIG_SCHEMAS[name])
                errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
                self.assertEqual([], [error.message for error in errors], name)
            workspace_errors = sorted(
                Draft202012Validator(WORKSPACE_SCHEMA).iter_errors(workspace),
                key=lambda error: list(error.path),
            )
            self.assertEqual([], [error.message for error in workspace_errors], "workspace.json")
            registry_errors = sorted(
                Draft202012Validator(WORKFLOW_REGISTRY_SCHEMA).iter_errors(registry),
                key=lambda error: list(error.path),
            )
            self.assertEqual([], [error.message for error in registry_errors], "workflow_registry.json")
            current_errors = sorted(
                Draft202012Validator(CURRENT_WORKFLOW_SCHEMA).iter_errors(current),
                key=lambda error: list(error.path),
            )
            self.assertEqual([], [error.message for error in current_errors], "current_workflow.json")

            workflow = configs["workflow.json"]
            agent_runners = configs["agent_runners.json"]
            dashboard = configs["dashboard.json"]
            security = configs["security.json"]
            version_control = configs["version_control.json"]
            schema_version = configs["schema_version.json"]

            self.assertEqual(agent_runners["default_runner"], workflow["default_worker_runner"])
            self.assertEqual(workflow["version_control_config_file"], f"{root_value}/config/version_control.json")
            self.assertEqual(workflow["planning"]["planner_runner"], "planner")
            self.assertEqual(workflow["planning"]["auditor_runner"], "auditor")
            self.assertEqual(workflow["planning"]["task_granularity"], "coarse_by_default")
            self.assertEqual(workflow["planning"]["max_initial_tasks"], 8)
            self.assertTrue(workflow["planning"]["batch_low_risk_tasks"])
            self.assertEqual(workflow["validation"]["validator_agent_mode"], "on_deterministic_failure")
            self.assertFalse(workflow["validation"]["validator_agent_for_high_risk"])
            self.assertTrue(workflow["execution"]["continue_on_fail"])
            self.assertTrue(workflow["execution"]["recovery_before_new_work"])
            self_expansion = workflow["self_expansion"]
            for budget_key in (
                "max_cycles",
                "max_tasks_added_total",
                "max_tasks_per_cycle",
                "max_repeated_signature_count",
            ):
                self.assertEqual(self_expansion[budget_key], DEFAULT_SELF_EXPANSION_POLICY[budget_key])
                self.assertEqual(self_expansion[budget_key], 100)
            self.assertEqual(dashboard["port"], "auto")
            self.assertEqual(dashboard["preferred_port"], 3766)
            self.assertEqual(dashboard["port_range"], [3766, 4766])
            self.assertEqual(dashboard["server_state_file"], f"{root_value}/runtime/dashboard_server.json")
            self.assertFalse(version_control["checkpoint_policy"]["before_worker_run"])
            self.assertTrue(version_control["checkpoint_policy"]["after_validation_pass"])

            runners = agent_runners["runners"]
            worker_failover = agent_runners["runner_failover"]["worker"]
            self.assertEqual(worker_failover["strategy"], "ordered")
            self.assertEqual(worker_failover["runners"], ["worker", "worker_fallback"])
            self.assertEqual(worker_failover["mark_unhealthy_after"], 4)
            self.assertEqual(worker_failover["failure_window_seconds"], 900)
            self.assertEqual(runners["worker"]["prompt_delivery"]["mode"], "file_argument")
            self.assertEqual(runners["planner"]["inherits"], "worker")
            self.assertEqual(runners["auditor"]["timeout_seconds"], 21600)
            self.assertEqual(runners["validator"]["role"], "validator")
            self.assertEqual(runners["validator"]["timeout_seconds"], 900)
            self.assertNotIn("enabled", runners["validator"])
            self.assertEqual(runners["change_request_planner"]["role"], "change_request_planner")
            self.assertEqual(runners["summary"]["role"], "summary")
            self.assertEqual(runners["summary"]["inherits"], "worker")
            self.assertTrue(runners["summary"]["enabled"])
            self.assertEqual(runners["summary"]["timeout_seconds"], 900)
            self.assertEqual(runners["final_reviewer"]["role"], "final_reviewer")
            self.assertEqual(runners["final_reviewer"]["timeout_seconds"], 900)
            self.assertNotIn("enabled", runners["final_reviewer"])
            self.assertEqual(runners["inspector"]["inherits"], "worker")
            self.assertNotIn("adapter", runners["inspector"])
            self.assertEqual(runners["inspector"]["inherits"], "worker")
            self.assertTrue(runners["inspector"]["permission_policy"]["allow_command_execution"])
            self.assertTrue(runners["inspector"]["permission_policy"]["allow_project_file_edit"])
            self.assertFalse(runners["inspector"]["permission_policy"]["read_only"])
            self.assertFalse(runners["inspector"]["permission_policy"]["require_approval_for_risky_commands"])
            for runner_id in ("worker", "worker_fallback"):
                worker_policy = runners[runner_id]["permission_policy"]
                self.assertEqual(worker_policy["git_boundary_policy"], "unattended_full_access")
                self.assertFalse(worker_policy["require_approval_for_risky_commands"])
                self.assertEqual(
                    tuple(worker_policy["allowed_git_read_only_operations"]),
                    WORKER_GIT_READ_ONLY_OPERATIONS,
                )
                self.assertEqual(
                    tuple(worker_policy["denied_git_write_operations"]),
                    WORKER_DENIED_GIT_OPERATIONS,
                )
                self.assertEqual(
                    tuple(worker_policy["version_control_manager_only_operations"]),
                    VERSION_CONTROL_MANAGER_ONLY_OPERATIONS,
                )
                self.assertEqual(
                    worker_policy["adapter_enforcement"],
                    "runtime.adapters.policy.enforce_command_policy",
                )

            self.assertEqual(dashboard["read_models_dir"], workflow["read_models_dir"])
            self.assertTrue(security["dashboard"]["require_token"])
            self.assertFalse(security["approval"]["enabled"])
            self.assertEqual(security["approval"]["default_action_when_disabled"], "auto_authorize")
            git_policy = security["git_command_policy"]
            self.assertFalse(git_policy["enforce_worker_boundaries"])
            self.assertEqual(tuple(git_policy["worker_allowed_read_only_operations"]), WORKER_GIT_READ_ONLY_OPERATIONS)
            self.assertEqual(tuple(git_policy["worker_denied_write_operations"]), WORKER_DENIED_GIT_OPERATIONS)
            self.assertEqual(
                tuple(git_policy["version_control_manager_only_operations"]),
                VERSION_CONTROL_MANAGER_ONLY_OPERATIONS,
            )
            self.assertEqual(git_policy["adapter_enforcement"], "runtime.adapters.policy.enforce_command_policy")
            self.assertFalse(security["file_access"]["allow_out_of_boundary_writes"])
            self.assertEqual(security["file_access"]["out_of_boundary_write_allowlist"], [])
            self.assertTrue(version_control["default_on"])
            self.assertTrue(version_control["no_remote_push"])
            self.assertEqual(version_control["gitignore_policy"]["mode"], "agent_maintained_lightweight_block")
            self.assertEqual(version_control["gitignore_policy"]["file"], ".gitignore")
            self.assertEqual(version_control["gitignore_policy"]["rules_prompt"], ".loopplane/prompts/git_tracking_init.md")
            self.assertTrue(version_control["gitignore_policy"]["preserve_user_rules_outside_managed_block"])
            self.assertEqual(version_control["run_metadata"], {"enabled": False, "detail_level": "status"})
            self.assertFalse(version_control["commit_policy"]["write_to_user_branch"])
            self.assertFalse(version_control["commit_policy"]["require_approval_for_user_branch_commit"])
            self.assertFalse(version_control["rollback_policy"]["rollback_requires_approval"])
            self.assertFalse(version_control["rollback_policy"]["never_auto_rollback_user_changes"])

            for name in CONFIG_FILES:
                expected_version = WORKSPACE_SCHEMA_VERSION if name == "schema_version.json" else SCHEMA_VERSION
                self.assertEqual(schema_version["files"][name], expected_version)
            self.assertEqual(schema_version["files"][f"{root_value}/PLAN.md"], SCHEMA_VERSION)
            self.assertEqual(schema_version["schema_version"], WORKSPACE_SCHEMA_VERSION)
            self.assertEqual(schema_version["compatibility"]["status"], "compatibility_tagged")
            self.assertEqual(schema_version["compatibility"]["legacy_schema_version"], SCHEMA_VERSION)
            self.assertIn(
                f"{root_value}/runtime/state.json",
                schema_version["compatibility"]["legacy_schema_version_files"],
            )

            schema_validation = validate_project_schemas(project)
            self.assertTrue(schema_validation["ok"], json.dumps(schema_validation, indent=2, sort_keys=True))
            self.assertIn(".loopplane/workspace.json", schema_validation["checked_files"])
            self.assertIn(".loopplane/workflow_registry.json", schema_validation["checked_files"])
            self.assertIn(".loopplane/current_workflow.json", schema_validation["checked_files"])
            self.assertIn(f"{root_value}/config/workflow.json", schema_validation["checked_files"])
            self.assertIn(f"{root_value}/runtime/state.json", schema_validation["checked_files"])
            self.assertIn("workspace.schema.json", schema_validation["schemas_used"])
            self.assertIn("workflow_registry.schema.json", schema_validation["schemas_used"])
            self.assertIn("current_workflow.schema.json", schema_validation["schemas_used"])

    def test_cli_init_refuses_existing_v16_workspace_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            args = ("init", "--project", str(project), "--brief", "Keep existing matching files.")

            first = self.run_loopplane(*args)
            _workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(project)
            brief_path = workflow_root / "PROJECT_BRIEF.md"
            brief_after_first = brief_path.read_text(encoding="utf-8")
            workspace_after_first = (project / ".loopplane" / "workspace.json").read_bytes()
            registry_after_first = (project / ".loopplane" / "workflow_registry.json").read_bytes()
            current_after_first = (project / ".loopplane" / "current_workflow.json").read_bytes()
            second = self.run_loopplane(*args)
            brief_after_second = brief_path.read_text(encoding="utf-8")
            workspace_after_second = (project / ".loopplane" / "workspace.json").read_bytes()
            registry_after_second = (project / ".loopplane" / "workflow_registry.json").read_bytes()
            current_after_second = (project / ".loopplane" / "current_workflow.json").read_bytes()

            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("existing LoopPlane workspace detected", second.stdout)
            self.assertIn(".loopplane/workspace.json", second.stdout)
            self.assertIn("loopplane status --project", second.stdout)
            self.assertIn("loopplane attach --project", second.stdout)
            self.assertIn("explicit workflow-history command", second.stdout)
            self.assertEqual(brief_after_first, brief_after_second)
            self.assertEqual(workspace_after_first, workspace_after_second)
            self.assertEqual(registry_after_first, registry_after_second)
            self.assertEqual(current_after_first, current_after_second)

    def test_cli_init_refuses_existing_v15_flat_instance_without_creating_v16_identity_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Existing v1.5 flat instance.")
            workflow_id = json.loads((project / ".loopplane" / "config" / "workflow.json").read_text(encoding="utf-8"))[
                "workflow_id"
            ]
            for relative in (
                ".loopplane/workspace.json",
                ".loopplane/workflow_registry.json",
                ".loopplane/current_workflow.json",
            ):
                (project / relative).unlink()
            instance_path = project / ".loopplane" / "config" / "instance.json"
            instance_path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "workflow_id": workflow_id,
                        "project_root": ".",
                        "workflow_root": ".loopplane",
                        "layout": "compatibility_flat",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            protected_paths = (
                project / "PROJECT_BRIEF.md",
                project / "PLAN.md",
                project / ".loopplane" / "config" / "workflow.json",
                instance_path,
                project / ".loopplane" / "runtime" / "state.json",
            )
            before = {path: path.read_bytes() for path in protected_paths}

            second = self.run_loopplane(
                "init",
                "--project",
                str(project),
                "--brief",
                "Existing v1.5 flat instance.",
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("existing LoopPlane workspace detected", second.stdout)
            self.assertIn(".loopplane/config/instance.json", second.stdout)
            self.assertIn("loopplane status --project", second.stdout)
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content, path.as_posix())
            self.assertFalse((project / ".loopplane" / "workspace.json").exists())
            self.assertFalse((project / ".loopplane" / "workflow_registry.json").exists())
            self.assertFalse((project / ".loopplane" / "current_workflow.json").exists())

    def test_init_preserves_existing_workflow_registry_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Preserve workflow registry history.")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            current_path = project / ".loopplane" / "current_workflow.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            current = json.loads(current_path.read_text(encoding="utf-8"))
            current["selection_reason"] = "manual_current_workflow_preservation_test"
            current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            archived_workflow_id = "wf_20260611_abcdef12"
            registry["workflows"].append(
                {
                    "workflow_id": archived_workflow_id,
                    "name": "archived follow-up",
                    "status": "archived",
                    "workflow_root": f".loopplane/workflows/{archived_workflow_id}",
                    "created_at": registry["generated_at"],
                    "last_seen_at": registry["generated_at"],
                    "plan_file": f".loopplane/workflows/{archived_workflow_id}/PLAN.md",
                    "read_models_dir": f".loopplane/workflows/{archived_workflow_id}/read_models",
                    "runtime_dir": f".loopplane/workflows/{archived_workflow_id}/runtime",
                    "requests_dir": f".loopplane/workflows/{archived_workflow_id}/requests",
                    "completion_marker": f".loopplane/workflows/{archived_workflow_id}/runtime/plan_loop_complete.json",
                    "read_only": False,
                    "archived": True,
                    "summary": {
                        "one_line": "Archived history preserved by second init.",
                        "tasks_total": 1,
                        "tasks_completed": 1,
                        "tasks_blocked": 0,
                    },
                }
            )
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            before = registry_path.read_bytes()
            current_before = current_path.read_bytes()

            second = init_project(project, "Preserve workflow registry history.")

            self.assertIn(".loopplane/workflow_registry.json", second.preserved)
            self.assertIn(".loopplane/current_workflow.json", second.preserved)
            self.assertEqual(registry_path.read_bytes(), before)
            self.assertEqual(current_path.read_bytes(), current_before)

    def test_init_accepts_allowed_workflow_history_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for status in WORKFLOW_HISTORY_STATUSES:
                project = Path(tmp) / f"project-{status}"
                init_project(project, f"Allowed registry status {status}.")
                registry_path = project / ".loopplane" / "workflow_registry.json"
                registry = json.loads(registry_path.read_text(encoding="utf-8"))
                registry["workflows"][0]["status"] = status
                registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

                validation = validate_project_schemas(project)
                repeat_init = init_project(project, f"Allowed registry status {status}.")

                self.assertTrue(validation["ok"], json.dumps(validation, indent=2, sort_keys=True))
                self.assertIn(".loopplane/workflow_registry.json", repeat_init.preserved)

    def test_init_rejects_unsupported_workflow_history_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            init_project(project, "Unsupported registry status.")
            registry_path = project / ".loopplane" / "workflow_registry.json"
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            registry["workflows"][0]["status"] = "initialized"
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            validation = validate_project_schemas(project)

            self.assertFalse(validation["ok"], json.dumps(validation, indent=2, sort_keys=True))
            self.assertIn("workflow-history status", "\n".join(validation["errors"]))
            with self.assertRaises(InitConflictError) as raised:
                init_project(project, "Unsupported registry status.")
            self.assertIn("unsupported workflow-history status", "\n".join(raised.exception.conflicts))

    def test_init_refuses_to_overwrite_existing_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            brief = project / "PROJECT_BRIEF.md"
            brief.write_text("Existing human-authored brief\n", encoding="utf-8")

            result = self.run_loopplane("init", "--project", str(project), "--brief", "Replacement brief")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(brief.read_text(encoding="utf-8"), "Existing human-authored brief\n")
            _workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(project)
            self.assertTrue((workflow_root / "PROJECT_BRIEF.md").is_file())
            self.assertIn("Replacement brief", (workflow_root / "PROJECT_BRIEF.md").read_text(encoding="utf-8"))
            self.assertTrue((project / ".git").is_dir())

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_init_auto_initializes_local_git_repository_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            result = self.run_loopplane("init", "--project", str(project), "--brief", "Initialize local Git.")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((project / ".git").is_dir())

            rev_parse = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "--is-inside-work-tree"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(rev_parse.returncode, 0, rev_parse.stderr + rev_parse.stdout)
            self.assertEqual(rev_parse.stdout.strip(), "true")

            _workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(project)
            state = json.loads((workflow_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            workflow_status = json.loads(
                (workflow_root / "read_models" / "workflow_status.json").read_text(encoding="utf-8")
            )
            vc_status = json.loads(
                (workflow_root / "read_models" / "version_control_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "initialized")
            self.assertEqual(workflow_status["status"], "initialized")
            self.assertEqual(vc_status["status"], "ok")
            self.assertTrue(vc_status["repository"]["inside_work_tree"])
            self.assertEqual(vc_status["repository"]["root"], ".")
            self.assertIsNone(vc_status["problem"])

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_init_in_monorepo_subdirectory_records_distinct_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "monorepo"
            service = repo / "services" / "service-a"
            service.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "README.md").write_text("monorepo\n", encoding="utf-8")

            result = self.run_loopplane("init", "--project", str(service), "--brief", "Ship service A.")

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(service)
            expected_repo_root = os.path.relpath(repo.resolve(), start=service.resolve()).replace(os.sep, "/")
            self.assertEqual(workspace["project_root"], ".")
            self.assertEqual(workspace["repo_root"], expected_repo_root)
            self.assertNotEqual(workspace["repo_root"], workspace["project_root"])
            self.assertEqual(workspace["workspace_boundary"], "project_root")
            self.assertFalse(workspace["allow_out_of_boundary_writes"])

            validation = validate_project_schemas(service)
            self.assertTrue(validation["ok"], json.dumps(validation, indent=2, sort_keys=True))
            vc_status = json.loads(
                (workflow_root / "read_models" / "version_control_status.json").read_text(encoding="utf-8")
            )
            self.assertTrue(vc_status["repository"]["inside_work_tree"])
            self.assertEqual(vc_status["repository"]["root"], expected_repo_root)

            current = self.run_loopplane("workspace", "current", "--project", str(service), "--json")
            self.assertEqual(current.returncode, 0, current.stderr + current.stdout)
            payload = json.loads(current.stdout)
            self.assertEqual(payload["workspace_project_root"], ".")
            self.assertEqual(payload["repo_root"], expected_repo_root)
            self.assertEqual(payload["resolved_project_root"], service.resolve().as_posix())
            self.assertEqual(payload["resolved_repo_root"], repo.resolve().as_posix())
            self.assertEqual(payload["workspace_boundary"], "project_root")
            self.assertEqual(payload["resolved_workspace_boundary"], service.resolve().as_posix())
            self.assertFalse(payload["allow_out_of_boundary_writes"])

    def test_cli_init_warns_when_creating_child_workspace_inside_parent_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "parent-project"
            child = parent / "services" / "child-project"
            parent_result = self.run_loopplane("init", "--project", str(parent), "--brief", "Parent workspace.")

            self.assertEqual(parent_result.returncode, 0, parent_result.stderr + parent_result.stdout)

            child_result = self.run_loopplane("init", "--project", str(child), "--brief", "Child workspace.")

            self.assertEqual(child_result.returncode, 0, child_result.stderr + child_result.stdout)
            self.assertIn("warning: nested LoopPlane workspace context detected", child_result.stdout)
            self.assertIn("Nested LoopPlane parent workspace detected", child_result.stdout)
            self.assertIn(parent.resolve().as_posix(), child_result.stdout)
            self.assertIn("explicit namespace or approval", child_result.stdout)
            self.assertTrue((child / ".loopplane" / "workspace.json").is_file())

    def test_cli_init_inside_nested_parent_requires_explicit_target_or_allow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "parent-project"
            child = parent / "services" / "child-project"
            child.mkdir(parents=True)
            parent_result = self.run_loopplane("init", "--project", str(parent), "--brief", "Parent workspace.")

            self.assertEqual(parent_result.returncode, 0, parent_result.stderr + parent_result.stdout)

            blocked = self.run_loopplane("init", "--brief", "Child workspace.", cwd=child)

            self.assertEqual(blocked.returncode, EXIT_SECURITY_POLICY_VIOLATION, blocked.stderr + blocked.stdout)
            self.assertIn("nested workspace guard", blocked.stdout)
            self.assertIn("nested_workspace_requires_explicit_namespace", blocked.stdout)
            self.assertFalse((child / ".loopplane" / "workspace.json").exists())

            allowed = self.run_loopplane("init", "--brief", "Child workspace.", "--allow-nested-workspace", cwd=child)

            self.assertEqual(allowed.returncode, 0, allowed.stderr + allowed.stdout)
            self.assertTrue((child / ".loopplane" / "workspace.json").is_file())

    def test_init_surfaces_waiting_config_when_git_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            env = dict(os.environ)
            env["PATH"] = ""

            result = subprocess.run(
                [sys.executable, str(LoopPlane), "init", "--project", str(project), "--brief", "Git unavailable."],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Version control status: waiting_config", result.stdout)
            self.assertFalse((project / ".git").exists())

            _workspace, _registry, _current, _root_value, workflow_root = self.current_workflow_paths(project)
            state = json.loads((workflow_root / "runtime" / "state.json").read_text(encoding="utf-8"))
            workflow_status = json.loads(
                (workflow_root / "read_models" / "workflow_status.json").read_text(encoding="utf-8")
            )
            vc_status = json.loads(
                (workflow_root / "read_models" / "version_control_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "waiting_config")
            self.assertIn("version_control_unavailable", state["blocked_reasons"])
            self.assertEqual(workflow_status["status"], "waiting_config")
            self.assertEqual(vc_status["status"], "waiting_config")
            self.assertEqual(vc_status["problem"]["code"], "version_control_unavailable")
            self.assertEqual(vc_status["problem"]["reason"], "git_unavailable")

    def test_init_records_waiting_config_when_git_init_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"

            result = init_project(project, "Git init fails.", git_runner=FailingGitInitRunner())

            self.assertEqual(result.version_control_status, "waiting_config")
            self.assertEqual(result.version_control_problem, "version_control_unavailable")
            self.assertFalse((project / ".git").exists())

            state = json.loads((project / ".loopplane" / "runtime" / "state.json").read_text(encoding="utf-8"))
            vc_status = json.loads(
                (project / ".loopplane" / "read_models" / "version_control_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["status"], "waiting_config")
            self.assertIn("version_control_unavailable", state["blocked_reasons"])
            self.assertEqual(vc_status["status"], "waiting_config")
            self.assertEqual(vc_status["problem"]["code"], "version_control_unavailable")
            self.assertEqual(vc_status["problem"]["reason"], "git_init_failed")


if __name__ == "__main__":
    unittest.main()
