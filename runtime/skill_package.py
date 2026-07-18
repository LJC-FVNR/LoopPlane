from __future__ import annotations

import argparse
import ast
import base64
import binascii
import gzip
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from runtime.adapters.codex_executable_resolver import resolve_codex_executable
from runtime.agent_runner_cli import doctor_agent_runners
from runtime.agent_runners import (
    AgentRunnerConfigError,
    RunnerConfig,
    load_agent_runners,
    write_project_local_agent_runner_overrides,
)
from runtime.exit_codes import EXIT_INVALID_CONFIG, EXIT_SUCCESS
from runtime.init_workflow import InitConflictError, InitResult, init_project
from runtime.path_resolution import WORKFLOW_PATH_FIELDS, WorkflowPathError, WorkflowPaths, load_workflow_config
from runtime.schema_validation import (
    RUNTIME_VERSION,
    validate_project_schemas,
)
from runtime.workspace_identity import repository_root_value


SCHEMA_VERSION = "loopplane-skill-doctor-1"
PACKAGE_TREE_SCHEMA_VERSION = "loopplane-package-tree-check-20"
PACKAGE_METADATA_SCHEMA_VERSION = "loopplane-skill-package-1"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCHEMA_VERSION = "loopplane-skill-install-1"
UPDATE_SCHEMA_VERSION = "loopplane-skill-update-1"
PACK_SCHEMA_VERSION = "loopplane-skill-pack-1"
PACK_MANIFEST_SCHEMA_VERSION = "loopplane-skill-pack-manifest-1"
RUNNER_READINESS_SCHEMA_VERSION = "loopplane-runner-readiness-1"
AGENT_SKILL_PROJECTION_SCHEMA_VERSION = "loopplane-agent-skill-projection-1"
PROJECT_PACKAGE_MANIFEST_SCHEMA_VERSION = "loopplane-project-package-manifest-1"
INSTALL_BRIEF = (
    "Initialize a durable LoopPlane workflow instance for this project. "
    "Replace this bootstrap brief with the actual user request before planning."
)
INSTALL_INSTANCE_PROFILE = "compatibility_flat"
INSTALL_TOOL_VERSION = f"loopplane {RUNTIME_VERSION}"
INSTALL_TIME_CLI_ADAPTERS = {
    "codex_cli": "codex",
    "claude_code_cli": "claude",
}
INSTALL_TIME_DEFAULT_ADAPTER_ORDER = ("codex_cli", "claude_code_cli")
INSTALL_TIME_CLI_EXTENSION_ROOTS = (
    ".vscode-server/extensions",
    ".vscode/extensions",
    ".cursor-server/extensions",
    ".cursor/extensions",
)
RUNTIME_SCHEMA_PACKAGE_FILES = (
    "runtime/schemas/agent_runners.schema.json",
    "runtime/schemas/agent_runners_local.schema.json",
    "runtime/schemas/background_jobs.schema.json",
    "runtime/schemas/completion_marker.schema.json",
    "runtime/schemas/current_workflow.schema.json",
    "runtime/schemas/dashboard.schema.json",
    "runtime/schemas/dashboard_server.schema.json",
    "runtime/schemas/loopplane_home_config.schema.json",
    "runtime/schemas/loopplane_home_dashboard_servers.schema.json",
    "runtime/schemas/loopplane_home_workspaces.schema.json",
    "runtime/schemas/event_snapshot.schema.json",
    "runtime/schemas/evidence_manifest.schema.json",
    "runtime/schemas/expansion_proposal.schema.json",
    "runtime/schemas/expansion_registry.schema.json",
    "runtime/schemas/failure_registry.schema.json",
    "runtime/schemas/final_verification_report.schema.json",
    "runtime/schemas/git_ref_bundle_export_result.schema.json",
    "runtime/schemas/git_ref_bundle_import_result.schema.json",
    "runtime/schemas/health_report.schema.json",
    "runtime/schemas/migration_export_manifest.schema.json",
    "runtime/schemas/migration_import_result.schema.json",
    "runtime/schemas/nested_workspace_detection.schema.json",
    "runtime/schemas/nested_workspace_policy.schema.json",
    "runtime/schemas/objective_verification_report.schema.json",
    "runtime/schemas/preview_result.schema.json",
    "runtime/schemas/project_package_manifest.schema.json",
    "runtime/schemas/read_model_build_manifest.schema.json",
    "runtime/schemas/read_model_metrics.schema.json",
    "runtime/schemas/read_model_plan_index.schema.json",
    "runtime/schemas/read_model_run_detail.schema.json",
    "runtime/schemas/read_model_run_details_manifest.schema.json",
    "runtime/schemas/read_model_run_index.schema.json",
    "runtime/schemas/read_model_version_control_status.schema.json",
    "runtime/schemas/read_model_workflow_graph.schema.json",
    "runtime/schemas/read_model_workflow_status.schema.json",
    "runtime/schemas/runner_resource_lock.schema.json",
    "runtime/schemas/runtime_state.schema.json",
    "runtime/schemas/schema_migration_record.schema.json",
    "runtime/schemas/schema_version.schema.json",
    "runtime/schemas/security.schema.json",
    "runtime/schemas/self_expansion_policy.schema.json",
    "runtime/schemas/version_control.schema.json",
    "runtime/schemas/worker_write_boundary.schema.json",
    "runtime/schemas/template_instance.schema.json",
    "runtime/schemas/workflow.schema.json",
    "runtime/schemas/workflow_defaults.schema.json",
    "runtime/schemas/workflow_instance.schema.json",
    "runtime/schemas/workflow_preset.schema.json",
    "runtime/schemas/workflow_registry.schema.json",
    "runtime/schemas/workflow_template.schema.json",
    "runtime/schemas/workspace.schema.json",
)

PACKAGE_FILE_REQUIREMENT_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "name": "package_entrypoint_metadata",
        "label": "portable package entrypoint and metadata",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 Portable Skill Package layout",
        "files": (
            "SKILL.md",
            "README.md",
            "skill.json",
        ),
    },
    {
        "name": "codex_skill_interface_metadata",
        "label": "Codex skill interface metadata",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.5 Cross-agent compatibility; Codex agents/openai.yaml skill metadata",
        "files": (
            "agents/openai.yaml",
        ),
    },
    {
        "name": "reference_docs",
        "label": "portable package reference documents",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 references/",
        "files": (
            "references/README.md",
            "references/PROTOCOL.md",
            "references/RUNTIME_SPEC.md",
            "references/PLANNER_SPEC.md",
            "references/DASHBOARD_SPEC.md",
            "references/ADAPTERS.md",
            "references/SECURITY.md",
        ),
    },
    {
        "name": "prompt_and_project_templates",
        "label": "project and agent prompt templates",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 templates/",
        "files": (
            "templates/README.md",
            "templates/PROJECT_BRIEF.template.md",
            "templates/PLAN.template.md",
            "templates/SHARED_CONTEXT.template.md",
            "templates/worker_prompt.template.md",
            "templates/planner_prompt.template.md",
            "templates/auditor_prompt.template.md",
            "templates/recovery_prompt.template.md",
            "templates/inspector_prompt.template.md",
            "templates/validator_prompt.template.md",
            "templates/change_request_planner_prompt.template.md",
            "templates/summary_prompt.template.md",
            "templates/expansion_planner_prompt.template.md",
            "templates/objective_verifier_prompt.template.md",
            "templates/final_reviewer_prompt.template.md",
        ),
    },
    {
        "name": "command_scripts",
        "label": "portable command scripts",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 scripts/; LoopPlane.md 25 command surface",
        "files": (
            "scripts/README.md",
            "scripts/loopplane",
            "scripts/install_local.sh",
            "scripts/doctor.sh",
        ),
    },
    {
        "name": "release_validation_tools",
        "label": "release validation tools",
        "classification": "required",
        "spec_reference": "LoopPlane.md 28 Testing Plan; LoopPlane.md 26 MVP Scope",
        "files": (
            "scripts/check_package_tree.py",
        ),
    },
    {
        "name": "core_runtime_files",
        "label": "core runtime files from the portable layout",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 runtime/",
        "files": (
            "runtime/README.md",
            "runtime/__init__.py",
            "runtime/scheduler.py",
            "runtime/background_jobs.py",
            "runtime/watchdog.py",
            "runtime/prompt_builder.py",
            "runtime/validator.py",
            "runtime/reconciler.py",
            "runtime/final_verifier.py",
            "runtime/self_expansion.py",
            "runtime/plan_objectives.py",
            "runtime/objective_verification.py",
            "runtime/read_model_builder.py",
            "runtime/template_presets.py",
        ),
    },
    {
        "name": "runtime_adapter_files",
        "label": "runtime adapter files from the portable layout",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 runtime/adapters/",
        "files": (
            "runtime/adapters/base.py",
            "runtime/adapters/shell_adapter.py",
            "runtime/adapters/codex_cli_adapter.py",
            "runtime/adapters/codex_executable_resolver.py",
            "runtime/adapters/claude_code_cli_adapter.py",
            "runtime/adapters/noop_adapter.py",
            "runtime/adapters/README.md",
            "runtime/adapters/__init__.py",
        ),
    },
    {
        "name": "runtime_schema_files",
        "label": "JSON schemas for runtime, workspace, home, migration, and boundary metadata",
        "classification": "required",
        "spec_reference": "LoopPlane.md 9 Files and Data Models; LoopPlane.md 30.2 Schema validation; LoopPlane.md 31-33 v1.6 metadata",
        "files": RUNTIME_SCHEMA_PACKAGE_FILES,
    },
    {
        "name": "workflow_template_presets",
        "label": "builtin workflow template presets",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 templates/; deterministic workflow template preset feature",
        "files": (
            "templates/workflows/README.md",
            "templates/workflows/research-topic-exploration/template.json",
            "templates/workflows/research-topic-exploration/PROJECT_BRIEF.md.tpl",
            "templates/workflows/research-topic-exploration/SHARED_CONTEXT.md.tpl",
            "templates/workflows/research-topic-exploration/PLAN_DRAFT.md.tpl",
            "templates/workflows/research-topic-exploration/examples/minimal.preset.json",
            "templates/workflows/research-topic-exploration/examples/publication_grade.preset.json",
            "templates/workflows/dashboard-performance-investigation/template.json",
            "templates/workflows/dashboard-performance-investigation/PROJECT_BRIEF.md.tpl",
            "templates/workflows/dashboard-performance-investigation/SHARED_CONTEXT.md.tpl",
            "templates/workflows/dashboard-performance-investigation/PLAN_DRAFT.md.tpl",
            "templates/workflows/dashboard-performance-investigation/examples/local_dashboard_latency.preset.json",
        ),
    },
    {
        "name": "dashboard_server_package_files",
        "label": "dashboard server/static package files",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 dashboard/; LoopPlane.md 19-20 dashboard; R5 server mode",
        "reason": (
            "Dashboard server mode is implemented, so the package must include "
            "the server runtime entry and public assets it serves."
        ),
        "files": (
            "runtime/dashboard.py",
            "dashboard/README.md",
            "dashboard/package.json",
            "dashboard/public/static_dashboard.css",
            "dashboard/public/static_dashboard.js",
            "dashboard/public/vendor/cytoscape.min.js",
        ),
    },
    {
        "name": "example_projects",
        "label": "portable package examples",
        "classification": "required",
        "spec_reference": "LoopPlane.md 7.2 examples/",
        "files": (
            "examples/README.md",
            "examples/minimal_project/README.md",
            "examples/python_project/README.md",
            "examples/research_project/README.md",
        ),
    },
)

INTENTIONALLY_OPTIONAL_PACKAGE_PATH_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "name": "cross_agent_compatibility_files",
        "label": "cross-agent compatibility files",
        "classification": "optional",
        "spec_reference": "LoopPlane.md 7.5 Cross-agent compatibility",
        "paths": (
            "CLAUDE.md",
        ),
        "reason": "LoopPlane.md 7.5 keeps CLAUDE.md as an optional project instruction file.",
    },
    {
        "name": "global_dashboard_discovery",
        "label": "global cross-workspace dashboard discovery",
        "classification": "deferred",
        "spec_reference": "LoopPlane.md 26.2 MVP may defer",
        "paths": (),
        "reason": "Global cross-workspace dashboard discovery is explicitly deferred from the MVP.",
    },
)

REQUIRED_PACKAGE_FILES = tuple(
    path
    for group in PACKAGE_FILE_REQUIREMENT_GROUPS
    for path in group["files"]
)

REQUIRED_PACKAGE_DIRS = (
    "agents",
    "references",
    "templates",
    "scripts",
    "runtime",
    "runtime/adapters",
    "dashboard",
    "dashboard/src",
    "dashboard/public",
    "examples",
    "examples/minimal_project",
    "examples/python_project",
    "examples/research_project",
)

EXECUTABLE_PACKAGE_FILES = (
    "scripts/loopplane",
    "scripts/install_local.sh",
    "scripts/doctor.sh",
)

EXPECTED_PACKAGE_ROOTS = (
    "agents",
    "references",
    "templates",
    "scripts",
    "runtime",
    "dashboard",
    "examples",
)

PACKAGE_TOP_LEVEL_FILES = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    "skill.json",
)

AGENT_SKILL_INSTALL_TARGETS = (
    {
        "agent_style": "codex",
        "label": "Codex project skill",
        "root": ".codex/skills",
    },
    {
        "agent_style": "claude_code",
        "label": "Claude Code project skill",
        "root": ".claude/skills",
    },
)
AGENT_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")

PACKAGE_EXCLUDED_ROOTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)

PACKAGE_EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".swp",
    ".tmp",
    "~",
)

REQUIRED_NON_STUB_CLI_COMMANDS = (
    ("skill", "doctor"),
    ("skill", "install"),
    ("skill", "update"),
    ("skill", "pack"),
    ("configure-agent",),
    ("doctor-agent",),
    ("init",),
    ("write-brief",),
    ("plan",),
    ("audit-plan",),
    ("activate-plan",),
    ("start",),
    ("run",),
    ("preview",),
    ("tick",),
    ("pause",),
    ("resume",),
    ("stop",),
    ("attach",),
    ("status",),
    ("health",),
    ("logs",),
    ("summarize",),
    ("rebuild-read-models",),
    ("migrate",),
    ("export",),
    ("import",),
    ("dashboard",),
    ("dashboard", "list"),
    ("workspace", "current"),
    ("workspace", "register"),
    ("workspace", "unregister"),
    ("workspace", "scan"),
    ("workspace", "list"),
    ("workspace", "doctor"),
    ("workflow",),
    ("workflow", "list"),
    ("workflow", "current"),
    ("workflow", "show"),
    ("workflow", "switch"),
    ("workflow", "create"),
    ("workflow", "archive"),
    ("workflow", "restore"),
    ("workflow", "fork"),
    ("ask",),
    ("change-request", "submit"),
    ("approvals",),
    ("approve",),
    ("reject",),
    ("vc", "status"),
    ("vc", "checkpoint"),
    ("vc", "diff"),
    ("vc", "log"),
    ("vc", "rollback"),
    ("vc", "export"),
    ("vc", "import"),
    ("vc", "doctor"),
)

DEFERRED_CLI_COMMANDS: tuple[dict[str, Any], ...] = ()

MVP_REQUIRED_RELEASE_ITEMS = (
    {
        "id": "portable_package_skeleton",
        "label": "portable package skeleton",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("required_files", "required_directories", "skill_metadata"),
    },
    {
        "id": "project_local_loopplane_instance",
        "label": "project-local .loopplane/ instance",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": (
            "skill install",
            "schema_validation",
            "v16_canonical_workflow_root_release_gate",
            "v15_flat_compatibility_release_gate",
            "v16_runtime_schema_version_release_gate",
            "loopplane_home_authority_separation_release_gate",
        ),
    },
    {
        "id": "project_brief_and_plan_templates",
        "label": "PROJECT_BRIEF.md and PLAN.md templates",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("templates/PROJECT_BRIEF.template.md", "templates/PLAN.template.md"),
    },
    {
        "id": "core_config_files",
        "label": "workflow, agent runner, dashboard, security, and version-control configs",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("schema_validation", "skill install"),
    },
    {
        "id": "planner_and_auditor_prompts",
        "label": "planner and auditor prompts",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("templates/planner_prompt.template.md", "templates/auditor_prompt.template.md"),
    },
    {
        "id": "shell_noop_and_cli_adapter",
        "label": "shell/noop adapter and at least one CLI agent adapter",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("required_adapters_no_notimplemented",),
    },
    {
        "id": "scheduler_runtime_controls",
        "label": "scheduler lock, active run lease, dry-run preview, and health probe",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("tests.test_scheduler", "tests.test_detached_runtime", "tests.test_health"),
    },
    {
        "id": "prepare_run_before_prompt_generation",
        "label": "prepare_run before prompt generation",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/scheduler.py", "runtime/prompt_builder.py"),
    },
    {
        "id": "worker_prompt_and_evidence_dirs",
        "label": "worker prompt and run-specific evidence directories",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("templates/worker_prompt.template.md", "runtime/scheduler.py"),
    },
    {
        "id": "validator_authoritative_validation",
        "label": "validator and authoritative validation.json",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/validator.py", "tests.test_validation"),
    },
    {
        "id": "reconciler_latest_json",
        "label": "reconciler and latest.json",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/reconciler.py", "tests.test_reconciliation"),
    },
    {
        "id": "event_log_state_git_checkpointing",
        "label": "event log, state.json, and default Git checkpointing",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/scheduler.py", "runtime/version_control.py"),
    },
    {
        "id": "failure_registry",
        "label": "failure registry",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/scheduler.py", "tests.test_scheduler"),
    },
    {
        "id": "final_verifier",
        "label": "final verifier",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/final_verifier.py", "tests.test_final_verifier"),
    },
    {
        "id": "read_model_builder",
        "label": "read model builder",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": ("runtime/read_model_builder.py", "tests.test_read_models"),
    },
    {
        "id": "dashboard_history_switching",
        "label": "minimal dashboard or static dashboard with same-workspace workflow history switching",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": (
            "dashboard/README.md",
            "runtime/dashboard.py",
            "tests.test_dashboard",
            "dashboard_history_switching_release_gate",
            "archived_read_only_mutation_rejection_release_gate",
        ),
    },
    {
        "id": "workspace_registry_current_workflow_pointer",
        "label": "workspace registry and current workflow pointer",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": (
            "skill install",
            ".loopplane/workspace.json",
            ".loopplane/workflow_registry.json",
            ".loopplane/current_workflow.json",
            "workspace_registry_current_pointer_release_gate",
        ),
    },
    {
        "id": "control_request_protocol",
        "label": "control request protocol",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": (
            "runtime/control.py",
            "tests.test_control",
            "archived_read_only_mutation_rejection_release_gate",
        ),
    },
    {
        "id": "change_request_protocol",
        "label": "change request protocol",
        "spec_reference": "LoopPlane.md 26.1",
        "validation": (
            "runtime/change_requests.py",
            "tests.test_change_requests",
            "archived_read_only_mutation_rejection_release_gate",
        ),
    },
    {
        "id": "basic_tests",
        "label": "basic tests listed in LoopPlane.md 26.1 and 28",
        "spec_reference": "LoopPlane.md 26.1; LoopPlane.md 28",
        "validation": ("python3 -m unittest discover -s tests",),
    },
)

MVP_ALLOWED_DEFERRED_RELEASE_ITEMS = (
    {
        "id": "multiple_concurrently_running_workflows_per_workspace",
        "label": "multiple concurrently running workflows per workspace",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "global_cross_workspace_dashboard_discovery",
        "label": "global cross-workspace dashboard discovery",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "parallel_worker_execution",
        "label": "parallel worker execution",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "cloud_deployment",
        "label": "cloud deployment",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "kubernetes_or_temporal_backend",
        "label": "Kubernetes or Temporal backend",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "advanced_graph_editing",
        "label": "advanced graph editing",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "full_semantic_llm_validation",
        "label": "full semantic LLM validation",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "complex_cost_accounting",
        "label": "complex cost accounting",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "multi_user_dashboard_authentication",
        "label": "multi-user dashboard authentication",
        "spec_reference": "LoopPlane.md 26.2",
    },
    {
        "id": "remote_browser_based_collaboration",
        "label": "remote browser-based collaboration",
        "spec_reference": "LoopPlane.md 26.2",
    },
)

ACCEPTED_MVP_DEFERRED_RELEASE_ITEMS = tuple(
    {
        **item,
        "reason": "Explicitly listed in LoopPlane.md 26.2 MVP may defer.",
    }
    for item in MVP_ALLOWED_DEFERRED_RELEASE_ITEMS
)

REQUIRED_NON_STUB_ADAPTERS = (
    {
        "adapter": "noop",
        "module": "runtime/adapters/noop_adapter.py",
        "class": "NoopAdapter",
        "required_methods": ("run", "doctor"),
    },
    {
        "adapter": "shell",
        "module": "runtime/adapters/shell_adapter.py",
        "class": "ShellAdapter",
        "required_methods": ("run", "doctor"),
    },
    {
        "adapter": "codex_cli",
        "module": "runtime/adapters/codex_cli_adapter.py",
        "class": "CodexCliAdapter",
        "required_methods": ("run", "doctor"),
    },
    {
        "adapter": "claude_code_cli",
        "module": "runtime/adapters/claude_code_cli_adapter.py",
        "class": "ClaudeCodeCliAdapter",
        "required_methods": ("run", "doctor"),
    },
)

ADAPTER_BASE_CONTRACT_MODULES = (
    "runtime/adapters/base.py",
)

RECOMMENDED_CLI_FIXTURE_FLOW_SCHEMA_VERSION = "loopplane-recommended-cli-fixture-flows-1"
DASHBOARD_HISTORY_SWITCHING_GATE_SCHEMA_VERSION = "loopplane-dashboard-history-switching-gate-1"
WORKSPACE_REGISTRY_CURRENT_POINTER_GATE_SCHEMA_VERSION = "loopplane-workspace-registry-current-pointer-gate-1"
V16_CANONICAL_WORKFLOW_ROOT_GATE_SCHEMA_VERSION = "loopplane-v16-canonical-workflow-root-gate-1"
V15_FLAT_COMPATIBILITY_GATE_SCHEMA_VERSION = "loopplane-v15-flat-compatibility-gate-1"
V16_RUNTIME_SCHEMA_VERSION_GATE_SCHEMA_VERSION = "loopplane-v16-runtime-schema-version-gate-1"
ARCHIVED_READ_ONLY_MUTATION_REJECTION_GATE_SCHEMA_VERSION = "loopplane-archived-read-only-mutation-rejection-gate-1"
LOOPPLANE_HOME_AUTHORITY_SEPARATION_GATE_SCHEMA_VERSION = "loopplane-home-authority-separation-gate-1"
MIGRATION_STALE_STATE_EXCLUSION_GATE_SCHEMA_VERSION = "loopplane-migration-stale-state-exclusion-gate-1"

RECOMMENDED_CLI_FIXTURE_FLOWS: tuple[dict[str, Any], ...] = (
    {
        "name": "codex_cli_worker_recommended_fixture",
        "adapter": "codex_cli",
        "runner_id": "worker",
        "role": "worker",
        "task_id": "P0.T001",
        "command": "codex",
        "fixture_executable": "codex",
        "prompt_delivery": {
            "mode": "file_argument",
            "argument_template": "{{prompt_path}}",
        },
        "expected_record": "codex_fixture_record.json",
        "expected_fixture": "codex",
        "expected_prompt_source": "stdin",
        "expected_final_prefix": "CODEX FINAL\nsource=stdin\n",
        "required_task_artifacts": (
            "agent_status.json",
            "artifacts/result.txt",
            "report.md",
            "commands.sh",
        ),
    },
    {
        "name": "claude_code_cli_worker_recommended_fixture",
        "adapter": "claude_code_cli",
        "runner_id": "worker_fallback",
        "role": "worker",
        "task_id": "P0.T002",
        "command": "claude",
        "fixture_executable": "claude",
        "prompt_delivery": {
            "mode": "stdin_or_prompt_flag",
            "prompt_file": "{{prompt_path}}",
        },
        "expected_record": "claude_fixture_record.json",
        "expected_fixture": "claude",
        "expected_prompt_source": "file",
        "expected_final_prefix": "CLAUDE FINAL\nsource=file\n",
        "required_task_artifacts": (
            "agent_status.json",
            "artifacts/result.txt",
            "report.md",
            "commands.sh",
        ),
    },
)

DOC_CONSISTENCY_SCHEMA_VERSION = "loopplane-docs-consistency-1"
DOC_SMOKE_EXAMPLES_SCHEMA_VERSION = "loopplane-docs-smoke-examples-1"
DOC_STATUS_CLASSIFICATION_SCHEMA_VERSION = "loopplane-docs-status-classification-1"
JSON_EXAMPLE_CHECK_SCHEMA_VERSION = "loopplane-json-example-check-1"
JSONL_EXAMPLE_CHECK_SCHEMA_VERSION = "loopplane-jsonl-example-check-1"

JSON_EXAMPLE_TOP_LEVEL_DOC_FILES = (
    "LoopPlane.md",
    "README.md",
)

JSON_EXAMPLE_MARKDOWN_ROOTS = (
    "references",
    "templates",
    "runtime",
    "runtime/adapters",
    "dashboard",
    "examples",
)

JSON_EXAMPLE_FILE_ROOTS = (
    "runtime/schemas",
    "references",
    "templates",
    "dashboard",
    "examples",
)

JSON_EXAMPLE_TOP_LEVEL_JSON_FILES = (
    "skill.json",
)

JSON_EXAMPLE_EVIDENCE_ROOT = ""
JSON_EXAMPLE_EVIDENCE_MIN_SECTION = 9
JSON_EXAMPLE_EVIDENCE_MAX_SECTION = 17
JSON_EXAMPLE_REQUIRED_LOOPPLANE_MD_FENCE_COUNT = 50
JSONL_EXAMPLE_REQUIRED_LOOPPLANE_MD_FENCE_COUNT = 5
JSON_EXAMPLE_FENCE_RE = re.compile(r"^```(?P<info>[^`\n]*)\n(?P<body>.*?)(?:^```\s*$)", re.MULTILINE | re.DOTALL)
JSON_EXAMPLE_HEADING_RE = re.compile(r"^(?P<level>#{2,6})\s+(?P<title>.*)$", re.MULTILINE)
JSON_EXAMPLE_EXCLUSION_RE = re.compile(r"loopplane-json-example-exclude:\s*(?P<reason>.+)", re.IGNORECASE)
JSONL_EXAMPLE_EXCLUSION_RE = re.compile(r"loopplane-jsonl-example-exclude:\s*(?P<reason>.+)", re.IGNORECASE)
JSON_EXAMPLE_SCHEMA_HEADINGS = (
    {
        "file": "LoopPlane.md",
        "heading_contains": "31.3 Workspace identity",
        "schema_file": "workspace.schema.json",
    },
    {
        "file": "LoopPlane.md",
        "heading_contains": "31.4 Workflow registry",
        "schema_file": "workflow_registry.schema.json",
    },
    {
        "file": "LoopPlane.md",
        "heading_contains": "31.5 Current workflow pointer",
        "schema_file": "current_workflow.schema.json",
    },
    {
        "file": "LoopPlane.md",
        "heading_contains": "32.3 Global workspace registry",
        "schema_file": "loopplane_home_workspaces.schema.json",
    },
    {
        "file": "LoopPlane.md",
        "heading_contains": "32.4 Dashboard port allocation",
        "schema_file": "dashboard_server.schema.json",
        "required_keys": ("server_mode", "api_base_url", "server_state_file"),
    },
)
JSON_EXAMPLE_SCHEMA_PATHS = (
    {
        "suffix": ".loopplane/workspace.json",
        "schema_file": "workspace.schema.json",
    },
    {
        "suffix": ".loopplane/workflow_registry.json",
        "schema_file": "workflow_registry.schema.json",
    },
    {
        "suffix": ".loopplane/current_workflow.json",
        "schema_file": "current_workflow.schema.json",
    },
    {
        "suffix": "registry/workspaces.json",
        "schema_file": "loopplane_home_workspaces.schema.json",
    },
    {
        "suffix": "runtime/dashboard_server.json",
        "schema_file": "dashboard_server.schema.json",
    },
    {
        "suffix": "archive_manifest.json",
        "schema_file": "migration_export_manifest.schema.json",
    },
)
JSON_EXAMPLE_SCHEMA_VERSIONS = {
    "loopplane-migration-import-1": "migration_import_result.schema.json",
    "loopplane-git-ref-bundle-export-1": "git_ref_bundle_export_result.schema.json",
    "loopplane-git-ref-bundle-import-1": "git_ref_bundle_import_result.schema.json",
}

PRIMARY_USER_DOC_FILES = (
    "LoopPlane.md",
    "README.md",
    "references/ADAPTERS.md",
    "runtime/adapters/README.md",
    "dashboard/README.md",
    "scripts/README.md",
)

STALE_COMPLETION_DOC_PATTERNS = (
    {"id": "reserved_until_implemented", "pattern": r"\breserved\s+until\s+implemented\b"},
    {"id": "reserved_for_future", "pattern": r"\breserved\s+for\s+(?:the\s+)?future\b"},
    {"id": "stub", "pattern": r"\bstubs?\b|\bstubbed\b|\bstub-only\b"},
    {"id": "skeleton", "pattern": r"\bskeleton\b|\bskeleton-only\b"},
    {"id": "not-implemented", "pattern": r"\bnot[-_\s]+implemented\b|\bnotimplementederror\b"},
    {"id": "deferred_implementation", "pattern": r"\bdeferred[-\s]+implementation\b"},
)

COMPLETED_REQUIREMENT_DOC_SURFACES: tuple[dict[str, Any], ...] = (
    {
        "id": "cli_agent_adapters",
        "label": "Codex and Claude CLI adapters",
        "completed_scope": "R1",
        "aliases": (
            "codex_cli",
            "claude_code_cli",
            "codex cli",
            "claude code cli",
            "cli agent adapter",
            "cli agent adapters",
            "cli adapter",
            "cli adapters",
            "agent runner adapter",
            "agent runner adapters",
        ),
    },
    {
        "id": "detached_runtime",
        "label": "detached runtime and supervisor",
        "completed_scope": "R2",
        "aliases": (
            "start --detach",
            "loopplane start --detach",
            "detached runtime",
            "detached scheduler",
            "supervisor",
        ),
    },
    {
        "id": "skill_commands",
        "label": "portable skill package commands",
        "completed_scope": "R3",
        "aliases": (
            "skill doctor",
            "skill install",
            "skill update",
            "skill pack",
            "portable skill package commands",
        ),
    },
    {
        "id": "version_control_commands",
        "label": "version-control CLI commands",
        "completed_scope": "R4",
        "aliases": (
            "vc status",
            "vc doctor",
            "vc checkpoint",
            "vc diff",
            "vc log",
            "vc rollback",
            "version-control commands",
            "version control commands",
        ),
    },
    {
        "id": "dashboard_server_api_controls",
        "label": "dashboard server, API, and control UI",
        "completed_scope": "R5",
        "aliases": (
            "dashboard --port",
            "dashboard server",
            "server mode",
            "dashboard api",
            "dashboard controls",
            "approval panel",
            "inspector console",
            "runner configuration ui",
        ),
    },
    {
        "id": "write_brief",
        "label": "write-brief command",
        "completed_scope": "R6",
        "aliases": (
            "write-brief",
            "loopplane write-brief",
        ),
    },
    {
        "id": "r7_release_gates",
        "label": "R7 release validation gates",
        "completed_scope": "R7",
        "aliases": (
            "check_package_tree.py",
            "package tree check",
            "release gate",
            "release gates",
            "release validation",
            "cli smoke",
            "smoke matrix",
        ),
    },
    {
        "id": "v16_workspace_workflow_history",
        "label": "same-workspace workflow history and dashboard switching",
        "completed_scope": "R9-R12, R17",
        "aliases": (
            "same-workspace workflow history",
            "workflow history",
            "workflow-history",
            "dashboard workflow switching",
            "dashboard --workflow",
            "workflow selector",
            "workflow_registry.json",
            "current_workflow.json",
            "workspace.json",
        ),
    },
    {
        "id": "v16_workspace_workflow_cli",
        "label": "workspace and workflow CLI groups",
        "completed_scope": "R11",
        "aliases": (
            "workspace current",
            "workspace register",
            "workspace unregister",
            "workspace scan",
            "workspace list",
            "workspace doctor",
            "workflow list",
            "workflow current",
            "workflow show",
            "workflow switch",
            "workflow create",
            "workflow archive",
            "workflow restore",
            "workflow fork",
            "workspace/workflow cli",
            "workspace and workflow cli",
        ),
    },
    {
        "id": "v16_loopplane_home_authority",
        "label": "LOOPPLANE_HOME discovery and local override authority",
        "completed_scope": "R13, R17",
        "aliases": (
            "loopplane_home",
            "loopplane home",
            "$loopplane_home",
            "global workspace registry",
            "registry/workspaces.json",
            "dashboard/servers.json",
            "agent_runners.local.json",
            "local override",
            "local overrides",
            "discovery-only",
            "discovery_only",
        ),
    },
    {
        "id": "v16_runner_locks",
        "label": "runner resource locks",
        "completed_scope": "R14",
        "aliases": (
            "runner resource lock",
            "runner resource locks",
            "machine-level runner lock",
            "machine-level runner locks",
            "runner_locks",
            "resource_policy",
            "lock_key",
        ),
    },
    {
        "id": "v16_workspace_boundaries",
        "label": "monorepo and nested workspace boundaries",
        "completed_scope": "R15",
        "aliases": (
            "monorepo",
            "nested workspace",
            "nested workspaces",
            "workspace boundary",
            "workspace boundaries",
            "workspace_namespace",
            "allow-nested-workspace",
        ),
    },
    {
        "id": "v16_migration_and_git_ref_bundles",
        "label": "migration export/import profiles and Git-ref bundles",
        "completed_scope": "R16-R17",
        "aliases": (
            "export --profile source",
            "export --profile stateful",
            "export --profile archive",
            "loopplane import",
            "migration export",
            "migration import",
            "migration profiles",
            "git-ref bundle",
            "git-ref bundles",
            "vc export",
            "vc import",
            "stale-state",
            "stale state",
        ),
    },
    {
        "id": "v16_archived_read_only_safeguards",
        "label": "archived/read-only workflow mutation safeguards",
        "completed_scope": "R17",
        "aliases": (
            "archived/read-only",
            "archived read-only",
            "read_only_imported",
            "read-only workflow",
            "read-only workflows",
            "mutation safeguards",
            "mutation rejection",
        ),
    },
)

SMOKE_EXAMPLE_DOC_FILES = (
    "LoopPlane.md",
    "README.md",
    "references/ADAPTERS.md",
    "runtime/adapters/README.md",
    "dashboard/README.md",
    "scripts/README.md",
    "runtime/README.md",
)

SMOKE_EXAMPLE_RISK_PATTERNS = (
    {
        "id": "noop_replaces_cli",
        "pattern": (
            r"\b(?:noop|no-op)\b.{0,80}"
            r"\b(?:replaces?|replacement\s+for|substitute\s+for|substitutes\s+for|"
            r"satisf(?:y|ies)\s+full|is\s+production|production\s+adapter)\b"
        ),
    },
    {
        "id": "shell_replaces_cli",
        "pattern": (
            r"\bshell(?:[-\s]+only)?\b.{0,80}"
            r"\b(?:replaces?|replacement\s+for|substitute\s+for|substitutes\s+for|"
            r"satisf(?:y|ies)\s+full|is\s+production|production\s+adapter)\b"
        ),
    },
    {
        "id": "static_dashboard_request_entry",
        "pattern": (
            r"\bstatic\s+dashboard\b.{0,120}"
            r"\b(?:supports|provides|enables|allows)\b.{0,80}"
            r"\b(?:request[-\s]+entry|mutating|writes?\s+requests?|start|pause|resume|"
            r"stop|approve|reject|change\s+request)\b"
        ),
    },
    {
        "id": "offline_dashboard_request_entry",
        "pattern": (
            r"\boffline(?:/static)?\b.{0,120}"
            r"\b(?:supports|provides|enables|allows)\b.{0,80}"
            r"\b(?:request[-\s]+entry|mutating|writes?\s+requests?)\b"
        ),
    },
    {
        "id": "fake_cli_provider_integration",
        "pattern": (
            r"\bfake\s+(?:codex|claude)\b.{0,100}"
            r"\b(?:production|user-facing\s+provider|real\s+provider\s+integration|"
            r"provider\s+integration)\b"
        ),
    },
    {
        "id": "disabled_planner_full_flow",
        "pattern": (
            r"\b(?:disabled\s+planner|disabled\s+auditor|planner/auditor\s+disabled|"
            r"planner\s+disabled|auditor\s+disabled)\b.{0,100}"
            r"\b(?:full\s+requirements?|production|recommended\s+path)\b"
        ),
    },
)

SMOKE_EXAMPLE_REQUIRED_CLARIFICATIONS = (
    {
        "id": "readme_real_cli_validation_path",
        "file": "README.md",
        "required_terms": (
            "full provider-backed validation",
            "codex_cli",
            "claude_code_cli",
            "installed and authenticated",
        ),
    },
    {
        "id": "readme_offline_smoke_fixture_boundary",
        "file": "README.md",
        "required_terms": (
            "offline release smoke",
            "noop",
            "shell",
            "smoke fixtures",
            "production agent substitutes",
        ),
    },
    {
        "id": "adapter_reference_noop_shell_boundary",
        "file": "references/ADAPTERS.md",
        "required_terms": (
            "deterministic local smoke",
            "do not by themselves satisfy",
            "cli agent adapter",
        ),
    },
    {
        "id": "adapter_reference_fake_cli_boundary",
        "file": "references/ADAPTERS.md",
        "required_terms": (
            "fake `codex` and `claude`",
            "fixture coverage",
            "not user-facing provider integrations",
        ),
    },
    {
        "id": "runtime_adapters_fake_cli_boundary",
        "file": "runtime/adapters/README.md",
        "required_terms": (
            "fake `codex` and `claude`",
            "fixture coverage",
            "not user-facing provider integrations",
        ),
    },
    {
        "id": "dashboard_static_server_boundary",
        "file": "dashboard/README.md",
        "required_terms": (
            "offline/static generation",
            "read-only",
            "server mode",
            "request-entry controls",
        ),
    },
    {
        "id": "scripts_command_surface_boundary",
        "file": "scripts/README.md",
        "required_terms": (
            "noop",
            "shell",
            "smoke fixtures",
            "codex_cli",
            "claude_code_cli",
            "static dashboard mode",
            "server dashboard mode",
        ),
    },
    {
        "id": "runtime_dashboard_boundary",
        "file": "runtime/README.md",
        "required_terms": (
            "static, read-only dashboard bundle",
            "server mode",
            "request-entry controls",
        ),
    },
)

STATUS_CLASSIFICATION_DOC_FILES = (
    "README.md",
    "scripts/README.md",
    "references/README.md",
    "runtime/README.md",
    "dashboard/README.md",
    "references/ADAPTERS.md",
    "runtime/adapters/README.md",
)

STATUS_CLASSIFICATION_REQUIRED_CLARIFICATIONS = (
    {
        "id": "readme_completed_standalone_mvp",
        "file": "README.md",
        "required_terms": (
            "Completed standalone/MVP functionality",
            "codex_cli",
            "claude_code_cli",
            "detached supervisor-backed runtime",
            "skill doctor/install/update/pack workflows",
            "release gates that block unfinished required command and adapter surfaces",
        ),
    },
    {
        "id": "readme_mvp_with_future_expansion",
        "file": "README.md",
        "required_terms": (
            "v1.6 Support Status",
            "same-workspace workflow history",
            "dashboard workflow switching",
            "workspace registry/current pointer support",
            "v1.5 flat compatibility",
            ".loopplane/workspace.json",
            ".loopplane/workflow_registry.json",
            ".loopplane/current_workflow.json",
            "canonical v1.6 workflow-root mode",
        ),
    },
    {
        "id": "readme_v16_support_status",
        "file": "README.md",
        "required_terms": (
            "workspace and workflow CLI groups",
            "LOOPPLANE_HOME discovery and local override support",
            "Runner resource locks",
            "Monorepo and nested workspace boundaries",
            "Migration export/import profiles",
            "Git-ref bundle export/import",
            "archived/read-only workflow mutation safeguards",
            "Release gates",
        ),
    },
    {
        "id": "readme_optional_spec_behaviors",
        "file": "README.md",
        "required_terms": (
            "Optional behavior allowed by the spec",
            "multiple active-running workflows",
            "global cross-workspace dashboard discovery",
            "parallel workers",
            "multi-user dashboard authentication",
        ),
    },
    {
        "id": "readme_smoke_fixture_status",
        "file": "README.md",
        "required_terms": (
            "Smoke and fixture paths",
            "noop",
            "shell",
            "fake `codex`/`claude` test binaries",
            "do not replace installed and authenticated",
        ),
    },
    {
        "id": "scripts_v16_support_status",
        "file": "scripts/README.md",
        "required_terms": (
            "v1.6 Support Status",
            "same-workspace workflow history",
            "workspace registry/current workflow pointer",
            "workspace and workflow CLI groups",
            "LOOPPLANE_HOME discovery/local override",
            "runner resource lock",
            "monorepo and nested workspace boundary",
            "migration export/import profiles",
            "Git-ref bundle export/import",
            "archived/read-only workflow mutation safeguards",
        ),
    },
    {
        "id": "references_v16_support_status",
        "file": "references/README.md",
        "required_terms": (
            "v1.6 Support Status",
            "same-workspace workflow history",
            "dashboard workflow switching",
            "workspace registry/current workflow pointer",
            "v1.5 flat compatibility",
            "LOOPPLANE_HOME discovery/local override authority",
            "runner resource locks",
            "migration export/import profiles",
            "Git-ref bundle export/import",
            "release gates",
        ),
    },
    {
        "id": "runtime_v16_support_status",
        "file": "runtime/README.md",
        "required_terms": (
            "v1.6 Runtime Support Status",
            "same-workspace workflow history",
            "project-local workspace registry/current workflow pointer truth",
            "v1.5 flat compatibility",
            "LOOPPLANE_HOME discovery/local overrides",
            "runner resource locks",
            "monorepo and nested workspace boundary enforcement",
            "Git-ref bundle export/import",
        ),
    },
)

STATUS_CLASSIFICATION_OVERCLAIM_PATTERNS = (
    {
        "id": "v16_workspace_history_complete_claim",
        "pattern": (
            r"\b(?:workspace(?:/workflow)?|workflow[-\s]+history|workflow\s+registry|"
            r"current\s+workflow|\.loopplane/workflows)\b.{0,140}"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b|"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b.{0,140}"
            r"\b(?:workspace(?:/workflow)?|workflow[-\s]+history|workflow\s+registry|"
            r"current\s+workflow|\.loopplane/workflows)\b"
        ),
    },
    {
        "id": "v16_dashboard_workspace_complete_claim",
        "pattern": (
            r"\b(?:dashboard\s+--workflow|dashboard\s+--port\s+auto|dashboard\s+list|"
            r"port[-\s]+auto)\b.{0,140}"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b|"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b.{0,140}"
            r"\b(?:dashboard\s+--workflow|dashboard\s+--port\s+auto|dashboard\s+list|"
            r"port[-\s]+auto)\b"
        ),
    },
    {
        "id": "v16_global_migration_complete_claim",
        "pattern": (
            r"\b(?:LOOPPLANE_HOME|migration|export/import|export\s+--profile|"
            r"vc\s+export|vc\s+import|runner\s+lock|monorepo|nested\s+workspace)\b.{0,140}"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b|"
            r"\b(?:fully\s+implemented|complete|completed|production[-\s]+ready)\b.{0,140}"
            r"\b(?:LOOPPLANE_HOME|migration|export/import|export\s+--profile|"
            r"vc\s+export|vc\s+import|runner\s+lock|monorepo|nested\s+workspace)\b"
        ),
    },
)

STATUS_CLASSIFICATION_BOUNDARY_TERMS = (
    "future work",
    "future v1.6",
    "remain future",
    "remains future",
    "tracked by r",
    "deferred",
    "incomplete",
    "not yet",
    "still has",
    "still have",
    "compatibility",
    "optional",
    "may defer",
)

ACCEPTABLE_FUTURE_DOC_CONTEXTS: tuple[dict[str, Any], ...] = (
    {
        "id": "v1_6_workspace_workflow_history",
        "label": "deferred v1.6 workspace/workflow-history commands",
        "aliases": (
            "loopplane workspace",
            "loopplane workflow",
            "workspace cli",
            "workflow history",
            "workflow-history",
            "same-workspace",
            ".loopplane/workflows",
            "workflow_registry.json",
            "current_workflow.json",
            "workspace.json",
        ),
    },
    {
        "id": "v1_6_migration_exports",
        "label": "deferred v1.6 migration/export commands",
        "aliases": (
            "loopplane export",
            "loopplane import",
            "migration/export",
        ),
    },
    {
        "id": "deferred_by_active_remaining_plan",
        "label": "explicit remaining-plan deferral",
        "aliases": (
            "outside the completed implementation phases",
            "outside completed implementation phases",
            "tracked by r",
            "loopplane.md 26.2",
            "mvp may defer",
            "deferred v1.6",
        ),
    },
    {
        "id": "adapter_extension_modes",
        "label": "adapter extension modes that intentionally wait for custom implementations",
        "aliases": (
            "interactive_terminal",
            "custom_adapter",
            "terminal-capable adapter",
            "extension path",
        ),
    },
    {
        "id": "portable_package_skeleton_spec_term",
        "label": "LoopPlane.md package skeleton spec terms",
        "aliases": (
            "portable package skeleton",
            "protocol package skeleton",
        ),
    },
)


class SkillInstallConflictError(RuntimeError):
    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("LoopPlane skill install would overwrite existing files")


class SkillUpdateConflictError(RuntimeError):
    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("LoopPlane skill update would overwrite protected project-local files")


@dataclass(frozen=True)
class _InstallWorkspace:
    workspace_id: str
    created_at: str
    existing: bool


def doctor_skill_package(package_root: Path | str | None = None) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    missing_files = _missing_paths(root, REQUIRED_PACKAGE_FILES, want_file=True)
    missing_dirs = _missing_paths(root, REQUIRED_PACKAGE_DIRS, want_file=False)
    if missing_files:
        errors.append(f"missing required package file(s): {', '.join(missing_files)}")
    if missing_dirs:
        errors.append(f"missing required package directory/directories: {', '.join(missing_dirs)}")

    checks.append(
        {
            "name": "required_files",
            "status": "pass" if not missing_files else "fail",
            "checked_count": len(REQUIRED_PACKAGE_FILES),
            "missing": missing_files,
        }
    )
    checks.append(
        {
            "name": "required_directories",
            "status": "pass" if not missing_dirs else "fail",
            "checked_count": len(REQUIRED_PACKAGE_DIRS),
            "missing": missing_dirs,
        }
    )

    package_file_coverage_check = check_package_file_coverage(root)
    if package_file_coverage_check["status"] != "pass":
        for group in package_file_coverage_check.get("missing_required_groups") or []:
            label = str(group.get("label") or group.get("name") or "required package group")
            missing = ", ".join(str(path) for path in group.get("missing") or [])
            spec_reference = str(group.get("spec_reference") or "")
            reference_suffix = f" ({spec_reference})" if spec_reference else ""
            errors.append(f"{label}{reference_suffix} missing required file(s): {missing}")
    checks.append(package_file_coverage_check)

    executable_status = _check_executable_files(root)
    non_executable = [entry["path"] for entry in executable_status if not entry["executable"]]
    if non_executable:
        errors.append(f"required package script(s) are not executable: {', '.join(non_executable)}")
    checks.append(
        {
            "name": "script_executability",
            "status": "pass" if not non_executable else "fail",
            "files": executable_status,
        }
    )

    release_classification_check = check_required_deferred_release_classification(root)
    if release_classification_check["status"] != "pass":
        unresolved = release_classification_check.get("unresolved_classification_problems") or []
        errors.append(
            "required/deferred release classification problem(s): "
            f"{len(unresolved)} unresolved"
        )
    checks.append(release_classification_check)

    command_handler_check = check_required_command_handlers(root)
    if command_handler_check["status"] != "pass":
        stubbed = command_handler_check.get("stubbed_required_commands") or []
        missing = command_handler_check.get("missing_required_commands") or []
        detail_parts = []
        if stubbed:
            detail_parts.append(f"stubbed required command handler(s): {', '.join(stubbed)}")
        if missing:
            detail_parts.append(f"missing required command handler(s): {', '.join(missing)}")
        errors.append("; ".join(detail_parts) or "required command handler release gate failed")
    checks.append(command_handler_check)

    adapter_check = check_required_adapters_no_notimplemented(root)
    if adapter_check["status"] != "pass":
        failed = adapter_check.get("failed_required_methods") or []
        missing = adapter_check.get("missing_required_adapters") or []
        detail_parts = []
        if failed:
            detail_parts.append(f"placeholder required adapter method(s): {', '.join(failed)}")
        if missing:
            detail_parts.append(f"missing required adapter(s): {', '.join(missing)}")
        errors.append("; ".join(detail_parts) or "required adapter implementation release gate failed")
    checks.append(adapter_check)

    fixture_flow_check = check_recommended_cli_fixture_flows(root)
    if fixture_flow_check["status"] != "pass":
        failed = fixture_flow_check.get("failed_flows") or []
        errors.append(
            (
                "recommended CLI fixture flow(s) cannot execute fixture tasks: "
                f"{', '.join(failed)}"
            )
            if failed
            else "recommended CLI fixture flow release gate failed"
        )
    checks.append(fixture_flow_check)

    dashboard_history_check = check_dashboard_history_switching_release_gate(root)
    if dashboard_history_check["status"] != "pass":
        problems = dashboard_history_check.get("problems") or []
        errors.append(
            (
                "same-workspace dashboard workflow history switching release gate failed: "
                f"{', '.join(problems)}"
            )
            if problems
            else "same-workspace dashboard workflow history switching release gate failed"
        )
    checks.append(dashboard_history_check)

    archived_read_only_check = check_archived_read_only_mutation_rejection_release_gate(root)
    if archived_read_only_check["status"] != "pass":
        problems = archived_read_only_check.get("problems") or []
        errors.append(
            (
                "archived/read-only mutation rejection release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "archived/read-only mutation rejection release gate failed"
        )
    checks.append(archived_read_only_check)

    workspace_pointer_check = check_workspace_registry_current_pointer_release_gate(root)
    if workspace_pointer_check["status"] != "pass":
        problems = workspace_pointer_check.get("problems") or []
        errors.append(
            (
                "workspace registry/current workflow pointer release gate failed: "
                f"{', '.join(problems)}"
            )
            if problems
            else "workspace registry/current workflow pointer release gate failed"
        )
    checks.append(workspace_pointer_check)

    canonical_workflow_root_check = check_v16_canonical_workflow_root_release_gate(root)
    if canonical_workflow_root_check["status"] != "pass":
        problems = canonical_workflow_root_check.get("problems") or []
        errors.append(
            (
                "canonical v1.6 workflow-root release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "canonical v1.6 workflow-root release gate failed"
        )
    checks.append(canonical_workflow_root_check)

    flat_compatibility_check = check_v15_flat_compatibility_release_gate(root)
    if flat_compatibility_check["status"] != "pass":
        problems = flat_compatibility_check.get("problems") or []
        errors.append(
            (
                "v1.5 flat compatibility release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "v1.5 flat compatibility release gate failed"
        )
    checks.append(flat_compatibility_check)

    loopplane_home_authority_check = check_loopplane_home_authority_separation_release_gate(root)
    if loopplane_home_authority_check["status"] != "pass":
        problems = loopplane_home_authority_check.get("problems") or []
        errors.append(
            (
                "LOOPPLANE_HOME authority separation release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "LOOPPLANE_HOME authority separation release gate failed"
        )
    checks.append(loopplane_home_authority_check)

    migration_stale_state_check = check_migration_stale_state_exclusion_release_gate(root)
    if migration_stale_state_check["status"] != "pass":
        problems = migration_stale_state_check.get("problems") or []
        errors.append(
            (
                "migration stale-state exclusion release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "migration stale-state exclusion release gate failed"
        )
    checks.append(migration_stale_state_check)

    json_example_check = check_v16_json_examples_parseable(root)
    if json_example_check["status"] != "pass":
        problems = json_example_check.get("errors") or []
        errors.append(
            (
                "v1.6 JSON example parse/schema release gate failed: "
                f"{'; '.join(str(problem) for problem in problems[:5])}"
            )
            if problems
            else "v1.6 JSON example parse/schema release gate failed"
        )
    checks.append(json_example_check)

    jsonl_example_check = check_v16_jsonl_examples_parseable(root)
    if jsonl_example_check["status"] != "pass":
        problems = jsonl_example_check.get("errors") or []
        errors.append(
            (
                "v1.6 JSONL example parse/schema release gate failed: "
                f"{'; '.join(str(problem) for problem in problems[:5])}"
            )
            if problems
            else "v1.6 JSONL example parse/schema release gate failed"
        )
    checks.append(jsonl_example_check)

    runtime_schema_version_check = check_v16_runtime_schema_version_release_gate(root)
    if runtime_schema_version_check["status"] != "pass":
        problems = runtime_schema_version_check.get("problems") or []
        errors.append(
            (
                "v1.6 runtime schema-version release gate failed: "
                f"{', '.join(str(problem) for problem in problems)}"
            )
            if problems
            else "v1.6 runtime schema-version release gate failed"
        )
    checks.append(runtime_schema_version_check)

    docs_check = check_docs_completed_requirements_not_stubbed(root)
    if docs_check["status"] != "pass":
        stale_claims = docs_check.get("stale_completed_requirement_claims") or []
        errors.append(
            "documentation consistency problem(s): "
            f"{len(stale_claims)} stale completed-requirement claim(s)"
        )
    checks.append(docs_check)

    smoke_docs_check = check_docs_smoke_examples_are_not_substitutes(root)
    if smoke_docs_check["status"] != "pass":
        risky_claims = smoke_docs_check.get("risky_substitute_claims") or []
        missing_clarifications = smoke_docs_check.get("missing_required_clarifications") or []
        detail_parts = []
        if risky_claims:
            detail_parts.append(f"{len(risky_claims)} risky smoke substitute claim(s)")
        if missing_clarifications:
            detail_parts.append(f"{len(missing_clarifications)} missing smoke-example clarification(s)")
        errors.append(
            "documentation smoke-example framing problem(s): "
            + (", ".join(detail_parts) if detail_parts else "release gate failed")
        )
    checks.append(smoke_docs_check)

    status_docs_check = check_docs_status_classification_language(root)
    if status_docs_check["status"] != "pass":
        overclaims = status_docs_check.get("future_overclaim_claims") or []
        missing_clarifications = status_docs_check.get("missing_required_clarifications") or []
        detail_parts = []
        if overclaims:
            detail_parts.append(f"{len(overclaims)} future-surface overclaim(s)")
        if missing_clarifications:
            detail_parts.append(f"{len(missing_clarifications)} missing status clarification(s)")
        errors.append(
            "documentation implementation-status classification problem(s): "
            + (", ".join(detail_parts) if detail_parts else "release gate failed")
        )
    checks.append(status_docs_check)

    metadata, metadata_errors, metadata_warnings = _check_metadata(root)
    errors.extend(metadata_errors)
    warnings.extend(metadata_warnings)
    checks.append(
        {
            "name": "skill_metadata",
            "status": "pass" if not metadata_errors else "fail",
            "metadata": metadata,
            "errors": metadata_errors,
            "warnings": metadata_warnings,
        }
    )

    agent_skill_entrypoint_check = _check_agent_skill_entrypoint_compatibility(root, metadata)
    if agent_skill_entrypoint_check["status"] != "pass":
        errors.extend(str(error) for error in agent_skill_entrypoint_check.get("errors") or [])
    checks.append(agent_skill_entrypoint_check)

    empty_files = _empty_required_text_files(root, missing_files)
    if empty_files:
        errors.append(f"required package file(s) are empty: {', '.join(empty_files)}")
    checks.append(
        {
            "name": "nonempty_required_files",
            "status": "pass" if not empty_files else "fail",
            "empty": empty_files,
        }
    )

    status = "pass" if not errors else "fail"
    return {
        "schema_version": SCHEMA_VERSION,
        "package_root": root.as_posix(),
        "status": status,
        "ok": status == "pass",
        "required_files_checked": list(REQUIRED_PACKAGE_FILES),
        "required_dirs_checked": list(REQUIRED_PACKAGE_DIRS),
        "missing_files": missing_files,
        "missing_dirs": missing_dirs,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def package_tree_check(package_root: Path | str | None = None) -> dict[str, Any]:
    result = doctor_skill_package(package_root)
    return {
        "schema_version": PACKAGE_TREE_SCHEMA_VERSION,
        "root": result["package_root"],
        "status": result["status"],
        "required_files_checked": result["required_files_checked"],
        "required_dirs_checked": result["required_dirs_checked"],
        "missing_files": result["missing_files"],
        "missing_dirs": result["missing_dirs"],
        "errors": result["errors"],
        "warnings": result["warnings"],
        "checks": result["checks"],
    }


def check_package_file_coverage(
    package_root: Path | str | None = None,
    *,
    groups: Sequence[Mapping[str, Any]] = PACKAGE_FILE_REQUIREMENT_GROUPS,
    optional_groups: Sequence[Mapping[str, Any]] = INTENTIONALLY_OPTIONAL_PACKAGE_PATH_GROUPS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    required_group_records: list[dict[str, Any]] = []
    optional_group_records: list[dict[str, Any]] = []
    missing_required_groups: list[dict[str, Any]] = []
    missing_required_files: list[str] = []

    for group in groups:
        files = _package_group_files(group)
        missing = _missing_paths(root, files, want_file=True)
        classification = str(group.get("classification") or "required")
        status = "pass"
        if classification == "required" and missing:
            status = "fail"
        elif missing:
            status = "optional_missing"
        record = {
            "name": str(group.get("name") or "unnamed_package_file_group"),
            "label": str(group.get("label") or group.get("name") or "package file group"),
            "classification": classification,
            "spec_reference": str(group.get("spec_reference") or ""),
            "reason": str(group.get("reason") or ""),
            "status": status,
            "checked_count": len(files),
            "files": list(files),
            "missing": missing,
        }
        required_group_records.append(record)
        if classification == "required" and missing:
            missing_required_groups.append(record)
            missing_required_files.extend(missing)

    for group in optional_groups:
        paths = _package_group_paths(group)
        present = [path for path in paths if (root / path.rstrip("/")).exists()]
        missing = [path for path in paths if path not in present]
        optional_group_records.append(
            {
                "name": str(group.get("name") or "unnamed_optional_package_path_group"),
                "label": str(group.get("label") or group.get("name") or "optional package path group"),
                "classification": str(group.get("classification") or "optional"),
                "spec_reference": str(group.get("spec_reference") or ""),
                "reason": str(group.get("reason") or ""),
                "status": "informational",
                "paths": list(paths),
                "present": present,
                "missing": missing,
            }
        )

    errors = [
        (
            f"{group['name']} missing required file(s): "
            f"{', '.join(str(path) for path in group.get('missing') or [])}"
        )
        for group in missing_required_groups
    ]
    return {
        "name": "package_file_coverage",
        "status": "pass" if not missing_required_groups else "fail",
        "spec_sources": [
            "LoopPlane.md 7.2 Portable Skill Package layout",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 28 Testing Plan",
        ],
        "required_groups": required_group_records,
        "optional_or_deferred_groups": optional_group_records,
        "missing_required_groups": missing_required_groups,
        "missing_required_files": sorted(set(missing_required_files)),
        "errors": errors,
    }


def check_v16_json_examples_parseable(
    package_root: Path | str | None = None,
    *,
    include_records: bool = False,
    markdown_files: Sequence[str] | None = None,
    json_files: Sequence[str] | None = None,
    required_loopplane_md_fence_count: int = JSON_EXAMPLE_REQUIRED_LOOPPLANE_MD_FENCE_COUNT,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    markdown_records: list[dict[str, Any]] = []
    json_file_records: list[dict[str, Any]] = []
    schema_validated: list[dict[str, Any]] = []
    excluded_markdown_records: list[dict[str, Any]] = []

    discovered_markdown_files = tuple(markdown_files) if markdown_files is not None else _json_example_markdown_files(root)
    for relative in discovered_markdown_files:
        path = root / relative
        if not path.is_file():
            errors.append(f"{relative}: markdown file is missing")
            continue
        text = path.read_text(encoding="utf-8")
        for record in _extract_markdown_json_examples(relative, text):
            if record["status"] == "excluded_invalid_json":
                excluded_markdown_records.append(record)
                markdown_records.append(record)
                continue
            if record["status"] != "parsed":
                errors.append(str(record["error"]))
                markdown_records.append(record)
                continue
            schema_file = _schema_for_markdown_json_example(record)
            if schema_file:
                schema_errors = _json_example_schema_errors(
                    root,
                    record["value"],
                    schema_file,
                    f"{relative}:{record['line']}",
                )
                record["schema_file"] = schema_file
                record["schema_status"] = "pass" if not schema_errors else "fail"
                if schema_errors:
                    record["schema_errors"] = schema_errors
                    errors.extend(schema_errors)
                else:
                    schema_validated.append(
                        {
                            "source": "markdown_fence",
                            "file": relative,
                            "line": record["line"],
                            "schema_file": schema_file,
                        }
                    )
            markdown_records.append(record)

    discovered_json_files = tuple(json_files) if json_files is not None else _json_example_json_files(root)
    for relative in discovered_json_files:
        path = root / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            errors.append(f"{relative}: invalid JSON at line {error.lineno}: {error.msg}")
            json_file_records.append(
                {
                    "source": "json_file",
                    "path": relative,
                    "status": "invalid_json",
                    "error": f"line {error.lineno}: {error.msg}",
                }
            )
            continue
        except OSError as error:
            errors.append(f"{relative}: read error: {type(error).__name__}: {error}")
            json_file_records.append(
                {
                    "source": "json_file",
                    "path": relative,
                    "status": "read_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue

        schema_checks = _schema_checks_for_json_file_example(relative, value)
        record = {
            "source": "json_file",
            "path": relative,
            "status": "parsed",
            "schema_checks": [],
        }
        for check in schema_checks:
            schema_file = str(check["schema_file"])
            location = str(check["location"])
            schema_errors = _json_example_schema_errors(root, check["value"], schema_file, location)
            check_record = {
                "location": location,
                "schema_file": schema_file,
                "schema_status": "pass" if not schema_errors else "fail",
            }
            if schema_errors:
                check_record["schema_errors"] = schema_errors
                errors.extend(schema_errors)
            else:
                schema_validated.append(
                    {
                        "source": "json_file",
                        "path": relative,
                        "location": location,
                        "schema_file": schema_file,
                    }
                )
            record["schema_checks"].append(check_record)
        json_file_records.append(record)

    loopplane_md_json_fences = sum(1 for record in markdown_records if record.get("file") == "LoopPlane.md")
    if loopplane_md_json_fences < required_loopplane_md_fence_count:
        errors.append(
            "LoopPlane.md JSON fence inventory is smaller than the v1.6 delta count: "
            f"{loopplane_md_json_fences} < {required_loopplane_md_fence_count}"
        )
    elif loopplane_md_json_fences > required_loopplane_md_fence_count:
        warnings.append(
            "LoopPlane.md JSON fence inventory exceeds the v1.6 delta count; "
            "all discovered fences were still parsed."
        )

    status = "pass" if not errors else "fail"
    result = {
        "name": "v16_json_examples_parseable",
        "schema_version": JSON_EXAMPLE_CHECK_SCHEMA_VERSION,
        "status": status,
        "spec_sources": [
            "LoopPlane.md 9 Files and Data Models",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 30.2 Schema validation",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
            "LoopPlane.md 32 Multi-Project Workspace Management Protocol",
            "LoopPlane.md 33 Installation and Migration Protocol",
            "LoopPlane.md 0 v1.6 Revision Notes",
        ],
        "markdown_files_checked": list(discovered_markdown_files),
        "json_file_roots_checked": list(JSON_EXAMPLE_FILE_ROOTS),
        "evidence_root_checked": None,
        "evidence_sections_checked": [],
        "schema_validation_scope": (
            "Markdown JSON examples are schema-checked when they map to current v1.6 runtime schemas. "
            "Release package JSON artifacts are parse-checked; development evidence archives are not required."
        ),
        "counts": {
            "markdown_files_checked": len(discovered_markdown_files),
            "markdown_json_fences": len(markdown_records),
            "loopplane_md_json_fences": loopplane_md_json_fences,
            "excluded_markdown_json_fences": len(excluded_markdown_records),
            "json_files_checked": len(discovered_json_files),
            "schema_validated_examples": len(schema_validated),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "schema_validated_examples": schema_validated if include_records else _limited_records(schema_validated, limit=40),
        "errors": errors,
        "warnings": warnings,
    }
    if include_records:
        result["markdown_examples"] = markdown_records
        result["json_file_examples"] = json_file_records
    return result


def check_v16_jsonl_examples_parseable(
    package_root: Path | str | None = None,
    *,
    include_records: bool = False,
    markdown_files: Sequence[str] | None = None,
    jsonl_files: Sequence[str] | None = None,
    required_loopplane_md_fence_count: int = JSONL_EXAMPLE_REQUIRED_LOOPPLANE_MD_FENCE_COUNT,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []
    markdown_records: list[dict[str, Any]] = []
    jsonl_file_records: list[dict[str, Any]] = []
    schema_validated: list[dict[str, Any]] = []
    excluded_markdown_records: list[dict[str, Any]] = []
    markdown_jsonl_fences = 0
    loopplane_md_jsonl_fences = 0

    discovered_markdown_files = tuple(markdown_files) if markdown_files is not None else _json_example_markdown_files(root)
    for relative in discovered_markdown_files:
        path = root / relative
        if not path.is_file():
            errors.append(f"{relative}: markdown file is missing")
            continue
        text = path.read_text(encoding="utf-8")
        fence_count, records = _extract_markdown_jsonl_examples(relative, text)
        markdown_jsonl_fences += fence_count
        if relative == "LoopPlane.md":
            loopplane_md_jsonl_fences += fence_count
        for record in records:
            if record["status"] == "excluded_invalid_jsonl":
                excluded_markdown_records.append(record)
                markdown_records.append(record)
                continue
            if record["status"] != "parsed":
                errors.append(str(record["error"]))
                markdown_records.append(record)
                continue
            _apply_jsonl_schema_checks(
                root=root,
                record=record,
                location=f"{relative}:{record['line']}",
                schema_validated=schema_validated,
                errors=errors,
            )
            markdown_records.append(record)

    discovered_jsonl_files = tuple(jsonl_files) if jsonl_files is not None else _jsonl_example_jsonl_files(root)
    for relative in discovered_jsonl_files:
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"{relative}: read error: {type(error).__name__}: {error}")
            jsonl_file_records.append(
                {
                    "source": "jsonl_file",
                    "path": relative,
                    "status": "read_error",
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                message = f"{relative}:{line_number}: invalid JSONL record: {error.msg}"
                errors.append(message)
                jsonl_file_records.append(
                    {
                        "source": "jsonl_file",
                        "path": relative,
                        "line": line_number,
                        "status": "invalid_jsonl",
                        "error": message,
                    }
                )
                continue
            record = {
                "source": "jsonl_file",
                "path": relative,
                "line": line_number,
                "status": "parsed",
                "value": value,
                "schema_checks": [],
            }
            _apply_jsonl_schema_checks(
                root=root,
                record=record,
                location=f"{relative}:{line_number}",
                schema_validated=schema_validated,
                errors=errors,
            )
            jsonl_file_records.append(record)

    if loopplane_md_jsonl_fences < required_loopplane_md_fence_count:
        errors.append(
            "LoopPlane.md JSONL fence inventory is smaller than the v1.6 delta count: "
            f"{loopplane_md_jsonl_fences} < {required_loopplane_md_fence_count}"
        )
    elif loopplane_md_jsonl_fences > required_loopplane_md_fence_count:
        warnings.append(
            "LoopPlane.md JSONL fence inventory exceeds the v1.6 delta count; "
            "all discovered fences were still parsed."
        )

    status = "pass" if not errors else "fail"
    result = {
        "name": "v16_jsonl_examples_parseable",
        "schema_version": JSONL_EXAMPLE_CHECK_SCHEMA_VERSION,
        "status": status,
        "spec_sources": [
            "LoopPlane.md 9 Files and Data Models",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 30.2 Schema validation",
            "LoopPlane.md 0 v1.6 Revision Notes",
        ],
        "markdown_files_checked": list(discovered_markdown_files),
        "jsonl_file_roots_checked": list(JSON_EXAMPLE_MARKDOWN_ROOTS),
        "evidence_root_checked": None,
        "evidence_sections_checked": [],
        "schema_validation_scope": (
            "JSONL records are schema-checked when a record schema_version maps to a current "
            "runtime schema. Current LoopPlane.md JSONL record types do not yet have dedicated "
            "runtime record schemas, so this gate primarily enforces line-parseability."
        ),
        "counts": {
            "markdown_files_checked": len(discovered_markdown_files),
            "markdown_jsonl_fences": markdown_jsonl_fences,
            "loopplane_md_jsonl_fences": loopplane_md_jsonl_fences,
            "markdown_jsonl_records": len(markdown_records),
            "excluded_markdown_jsonl_records": len(excluded_markdown_records),
            "jsonl_files_checked": len(discovered_jsonl_files),
            "jsonl_file_records": len(jsonl_file_records),
            "schema_validated_records": len(schema_validated),
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "schema_validated_records": schema_validated if include_records else _limited_records(schema_validated, limit=40),
        "errors": errors,
        "warnings": warnings,
    }
    if include_records:
        result["markdown_jsonl_records"] = markdown_records
        result["jsonl_file_records"] = jsonl_file_records
    return result


def _json_example_markdown_files(root: Path) -> tuple[str, ...]:
    files: set[str] = {relative for relative in JSON_EXAMPLE_TOP_LEVEL_DOC_FILES if (root / relative).is_file()}
    for root_relative in JSON_EXAMPLE_MARKDOWN_ROOTS:
        base = root / root_relative
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            if _is_package_ignored_path(path):
                continue
            files.add(_relative_posix(root, path))
    return tuple(sorted(files))


def _json_example_json_files(root: Path) -> tuple[str, ...]:
    files: set[str] = {relative for relative in JSON_EXAMPLE_TOP_LEVEL_JSON_FILES if (root / relative).is_file()}
    for root_relative in JSON_EXAMPLE_FILE_ROOTS:
        base = root / root_relative
        if not base.exists():
            continue
        for path in base.rglob("*.json"):
            if _is_package_ignored_path(path):
                continue
            files.add(_relative_posix(root, path))
    evidence_root = root / JSON_EXAMPLE_EVIDENCE_ROOT if JSON_EXAMPLE_EVIDENCE_ROOT else None
    if evidence_root is not None and evidence_root.exists():
        for path in evidence_root.rglob("*.json"):
            if not _is_v16_evidence_json_path(root, path):
                continue
            files.add(_relative_posix(root, path))
    return tuple(sorted(files))


def _jsonl_example_jsonl_files(root: Path) -> tuple[str, ...]:
    files: set[str] = set()
    for root_relative in JSON_EXAMPLE_MARKDOWN_ROOTS:
        base = root / root_relative
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            if _is_package_ignored_path(path):
                continue
            files.add(_relative_posix(root, path))
    evidence_root = root / JSON_EXAMPLE_EVIDENCE_ROOT if JSON_EXAMPLE_EVIDENCE_ROOT else None
    if evidence_root is not None and evidence_root.exists():
        for path in evidence_root.rglob("*.jsonl"):
            if not _is_v16_evidence_json_path(root, path):
                continue
            files.add(_relative_posix(root, path))
    return tuple(sorted(files))


def _extract_markdown_json_examples(relative: str, text: str) -> list[dict[str, Any]]:
    heading_positions = [
        (match.start(), match.group("title").strip())
        for match in JSON_EXAMPLE_HEADING_RE.finditer(text)
    ]
    records: list[dict[str, Any]] = []
    for match in JSON_EXAMPLE_FENCE_RE.finditer(text):
        info = match.group("info").strip()
        language = info.split()[0].lower() if info else ""
        if language != "json":
            continue
        line = text[: match.start()].count("\n") + 1
        body = match.group("body")
        exclusion_reason = _json_example_exclusion_reason(text, match.start(), info)
        heading = _json_example_heading_for(heading_positions, match.start())
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            record = {
                "source": "markdown_fence",
                "file": relative,
                "line": line,
                "language": language,
                "heading": heading,
            }
            if exclusion_reason:
                record.update(
                    {
                        "status": "excluded_invalid_json",
                        "exclusion_reason": exclusion_reason,
                        "parse_error": f"line {error.lineno}: {error.msg}",
                    }
                )
            else:
                record.update(
                    {
                        "status": "invalid_json",
                        "error": f"{relative}:{line}: invalid JSON: line {error.lineno}: {error.msg}",
                    }
                )
            records.append(record)
            continue
        records.append(
            {
                "source": "markdown_fence",
                "file": relative,
                "line": line,
                "language": language,
                "heading": heading,
                "status": "parsed",
                "value": value,
            }
        )
    return records


def _extract_markdown_jsonl_examples(relative: str, text: str) -> tuple[int, list[dict[str, Any]]]:
    heading_positions = [
        (match.start(), match.group("title").strip())
        for match in JSON_EXAMPLE_HEADING_RE.finditer(text)
    ]
    records: list[dict[str, Any]] = []
    fence_count = 0
    for match in JSON_EXAMPLE_FENCE_RE.finditer(text):
        info = match.group("info").strip()
        language = info.split()[0].lower() if info else ""
        if language != "jsonl":
            continue
        fence_count += 1
        fence_line = text[: match.start()].count("\n") + 1
        body = match.group("body")
        exclusion_reason = _jsonl_example_exclusion_reason(text, match.start(), info)
        heading = _json_example_heading_for(heading_positions, match.start())
        for offset, line_text in enumerate(body.splitlines(), start=1):
            if not line_text.strip():
                continue
            line = fence_line + offset
            base_record = {
                "source": "markdown_jsonl_fence",
                "file": relative,
                "fence_line": fence_line,
                "line": line,
                "language": language,
                "heading": heading,
            }
            try:
                value = json.loads(line_text)
            except json.JSONDecodeError as error:
                if exclusion_reason:
                    records.append(
                        {
                            **base_record,
                            "status": "excluded_invalid_jsonl",
                            "exclusion_reason": exclusion_reason,
                            "parse_error": f"line {error.lineno}: {error.msg}",
                        }
                    )
                else:
                    records.append(
                        {
                            **base_record,
                            "status": "invalid_jsonl",
                            "error": f"{relative}:{line}: invalid JSONL record: {error.msg}",
                        }
                    )
                continue
            records.append(
                {
                    **base_record,
                    "status": "parsed",
                    "value": value,
                    "schema_checks": [],
                }
            )
    return fence_count, records


def _json_example_exclusion_reason(text: str, fence_start: int, info: str) -> str:
    info_match = JSON_EXAMPLE_EXCLUSION_RE.search(info)
    if info_match:
        return info_match.group("reason").strip()
    prefix = text[:fence_start]
    previous_lines = prefix.splitlines()[-4:]
    for line in reversed(previous_lines):
        match = JSON_EXAMPLE_EXCLUSION_RE.search(line)
        if match:
            return match.group("reason").strip()
    return ""


def _jsonl_example_exclusion_reason(text: str, fence_start: int, info: str) -> str:
    for pattern in (JSONL_EXAMPLE_EXCLUSION_RE, JSON_EXAMPLE_EXCLUSION_RE):
        info_match = pattern.search(info)
        if info_match:
            return info_match.group("reason").strip()
    prefix = text[:fence_start]
    previous_lines = prefix.splitlines()[-4:]
    for line in reversed(previous_lines):
        for pattern in (JSONL_EXAMPLE_EXCLUSION_RE, JSON_EXAMPLE_EXCLUSION_RE):
            match = pattern.search(line)
            if match:
                return match.group("reason").strip()
    return ""


def _json_example_heading_for(heading_positions: Sequence[tuple[int, str]], position: int) -> str:
    heading = ""
    for heading_position, title in heading_positions:
        if heading_position < position:
            heading = title
        else:
            break
    return heading


def _schema_for_markdown_json_example(record: Mapping[str, Any]) -> str | None:
    file_name = str(record.get("file") or "")
    heading = str(record.get("heading") or "")
    value = record.get("value")
    if not isinstance(value, Mapping):
        return None
    for rule in JSON_EXAMPLE_SCHEMA_HEADINGS:
        if file_name != rule["file"]:
            continue
        if str(rule["heading_contains"]) not in heading:
            continue
        required_keys = rule.get("required_keys")
        if isinstance(required_keys, Sequence) and not isinstance(required_keys, (str, bytes)):
            if any(str(key) not in value for key in required_keys):
                continue
        return str(rule["schema_file"])
    return None


def _schema_checks_for_json_file_example(relative: str, value: Any) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(location: str, schema_file: str, instance: Any) -> None:
        key = (location, schema_file)
        if key in seen:
            return
        seen.add(key)
        checks.append({"location": location, "schema_file": schema_file, "value": instance})

    normalized = relative.replace("\\", "/")
    if JSON_EXAMPLE_EVIDENCE_ROOT and normalized.startswith(f"{JSON_EXAMPLE_EVIDENCE_ROOT}/"):
        return checks
    for rule in JSON_EXAMPLE_SCHEMA_PATHS:
        if normalized.endswith(str(rule["suffix"])):
            add(normalized, str(rule["schema_file"]), value)

    if isinstance(value, Mapping):
        schema_file = JSON_EXAMPLE_SCHEMA_VERSIONS.get(str(value.get("schema_version") or ""))
        if schema_file:
            add(normalized, schema_file, value)
        manifest = value.get("manifest")
        if isinstance(manifest, Mapping) and manifest.get("schema_version") == "loopplane-migration-export-1":
            add(f"{normalized}#manifest", "migration_export_manifest.schema.json", manifest)
    return checks


def _apply_jsonl_schema_checks(
    *,
    root: Path,
    record: dict[str, Any],
    location: str,
    schema_validated: list[dict[str, Any]],
    errors: list[str],
) -> None:
    value = record.get("value")
    for check in _schema_checks_for_jsonl_record_example(value):
        schema_file = str(check["schema_file"])
        schema_errors = _json_example_schema_errors(root, check["value"], schema_file, location)
        check_record = {
            "location": location,
            "schema_file": schema_file,
            "schema_status": "pass" if not schema_errors else "fail",
        }
        if schema_errors:
            check_record["schema_errors"] = schema_errors
            errors.extend(schema_errors)
        else:
            schema_validated.append(
                {
                    "source": str(record.get("source") or "jsonl_record"),
                    "file": str(record.get("file") or record.get("path") or ""),
                    "line": record.get("line"),
                    "schema_file": schema_file,
                }
            )
        record.setdefault("schema_checks", []).append(check_record)


def _schema_checks_for_jsonl_record_example(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    schema_file = JSON_EXAMPLE_SCHEMA_VERSIONS.get(str(value.get("schema_version") or ""))
    if not schema_file:
        return []
    return [{"schema_file": schema_file, "value": value}]


def _json_example_schema_errors(root: Path, value: Any, schema_file: str, location: str) -> list[str]:
    schema_path = root / "runtime" / "schemas" / schema_file
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{location}: unable to load schema {schema_file}: {type(error).__name__}: {error}"]
    try:
        from jsonschema import Draft202012Validator
    except ModuleNotFoundError as error:
        return [f"{location}: jsonschema is required for schema validation: {error}"]
    validator = Draft202012Validator(schema)
    return [
        f"{location}: {schema_file}: {error.message}"
        for error in sorted(validator.iter_errors(value), key=lambda item: list(item.path))
    ]


def _is_v16_evidence_json_path(root: Path, path: Path) -> bool:
    if not JSON_EXAMPLE_EVIDENCE_ROOT:
        return False
    try:
        parts = path.relative_to(root / JSON_EXAMPLE_EVIDENCE_ROOT).parts
    except ValueError:
        return False
    if not parts:
        return False
    first = parts[0]
    match = re.match(r"^r(?P<section>\d+)(?:_|$)", first)
    if not match:
        return False
    section = int(match.group("section"))
    return JSON_EXAMPLE_EVIDENCE_MIN_SECTION <= section <= JSON_EXAMPLE_EVIDENCE_MAX_SECTION


def _relative_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_package_ignored_path(path: Path) -> bool:
    return any(part in PACKAGE_EXCLUDED_ROOTS for part in path.parts)


def _limited_records(records: Sequence[Mapping[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    limited: list[dict[str, Any]] = []
    for record in records[:limit]:
        limited.append({key: value for key, value in record.items() if key != "value"})
    return limited


def check_required_deferred_release_classification(
    package_root: Path | str | None = None,
    *,
    required_items: Sequence[Mapping[str, Any]] = MVP_REQUIRED_RELEASE_ITEMS,
    allowed_deferred_items: Sequence[Mapping[str, Any]] = MVP_ALLOWED_DEFERRED_RELEASE_ITEMS,
    required_item_ids: Sequence[str] | None = None,
    deferred_items: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    required_catalog = _release_item_map(required_items)
    allowed_deferred_catalog = _release_item_map(allowed_deferred_items)
    classified_required_ids = tuple(
        str(item_id)
        for item_id in (required_item_ids if required_item_ids is not None else required_catalog)
        if str(item_id)
    )
    release_deferred_items = tuple(
        deferred_items if deferred_items is not None else ACCEPTED_MVP_DEFERRED_RELEASE_ITEMS
    )

    required_id_set = set(classified_required_ids)
    deferred_id_counts: dict[str, int] = {}
    unresolved: list[dict[str, Any]] = []
    accepted_deferrals: list[dict[str, Any]] = []

    for item_id in classified_required_ids:
        if item_id not in required_catalog:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "unknown_required_classification",
                    "classification": "required",
                    "message": f"{item_id} is classified as required but is not in LoopPlane.md 26.1 metadata.",
                }
            )

    for item_id, item in required_catalog.items():
        if item_id not in required_id_set:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "missing_required_classification",
                    "classification": "unclassified",
                    "label": str(item.get("label") or item_id),
                    "spec_reference": str(item.get("spec_reference") or ""),
                    "message": (
                        f"{item_id} is required by LoopPlane.md 26.1 and has no explicit "
                        "LoopPlane.md 26.2 deferral."
                    ),
                }
            )

    for entry in release_deferred_items:
        item_id = str(entry.get("id") or "")
        if not item_id:
            unresolved.append(
                {
                    "id": "",
                    "problem": "missing_deferred_item_id",
                    "classification": "deferred",
                    "message": "Deferred release item is missing an id.",
                }
            )
            continue
        deferred_id_counts[item_id] = deferred_id_counts.get(item_id, 0) + 1
        reason = str(entry.get("reason") or "")
        spec_reference = str(entry.get("spec_reference") or "")
        if item_id in required_catalog:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "required_item_marked_deferred",
                    "classification": "deferred",
                    "label": str(required_catalog[item_id].get("label") or item_id),
                    "spec_reference": str(required_catalog[item_id].get("spec_reference") or ""),
                    "message": (
                        f"{item_id} is required by LoopPlane.md 26.1 and cannot be accepted "
                        "as an MVP deferral."
                    ),
                }
            )
            continue
        allowed = allowed_deferred_catalog.get(item_id)
        if allowed is None:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "deferred_item_not_allowed_by_spec",
                    "classification": "deferred",
                    "message": f"{item_id} is deferred but is not listed in LoopPlane.md 26.2.",
                }
            )
            continue
        if not reason or not spec_reference:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "missing_deferred_spec_reference_or_reason",
                    "classification": "deferred",
                    "label": str(allowed.get("label") or item_id),
                    "spec_reference": spec_reference,
                    "message": (
                        f"{item_id} is an allowed deferral but must include both "
                        "spec_reference and reason."
                    ),
                }
            )
            continue
        accepted_deferrals.append(
            {
                "id": item_id,
                "label": str(allowed.get("label") or entry.get("label") or item_id),
                "spec_reference": spec_reference,
                "reason": reason,
            }
        )

    for item_id, count in sorted(deferred_id_counts.items()):
        if count > 1:
            unresolved.append(
                {
                    "id": item_id,
                    "problem": "duplicate_deferred_classification",
                    "classification": "deferred",
                    "message": f"{item_id} is listed as deferred {count} times.",
                }
            )

    overlap = sorted(required_id_set & set(deferred_id_counts))
    for item_id in overlap:
        unresolved.append(
            {
                "id": item_id,
                "problem": "required_deferred_overlap",
                "classification": "required,deferred",
                "message": f"{item_id} appears in both required and deferred classifications.",
            }
        )

    required_output = [
        {
            "id": item_id,
            "label": str(item.get("label") or item_id),
            "spec_reference": str(item.get("spec_reference") or ""),
            "validation": [str(value) for value in item.get("validation", ())],
        }
        for item_id, item in required_catalog.items()
        if item_id in required_id_set
    ]
    errors = [_release_problem_message(problem) for problem in unresolved]

    return {
        "name": "required_deferred_release_classification",
        "status": "pass" if not unresolved else "fail",
        "spec_sources": {
            "required": "LoopPlane.md 26.1 MVP must include",
            "deferred": "LoopPlane.md 26.2 MVP may defer",
            "testing": "LoopPlane.md 28 Testing Plan",
        },
        "spec_available": (root / "LoopPlane.md").is_file(),
        "required_items": required_output,
        "accepted_deferrals": accepted_deferrals,
        "unresolved_classification_problems": unresolved,
        "counts": {
            "required_items": len(required_output),
            "accepted_deferrals": len(accepted_deferrals),
            "unresolved_classification_problems": len(unresolved),
        },
        "errors": errors,
    }


def check_required_command_handlers(
    package_root: Path | str | None = None,
    *,
    required_commands: Sequence[Sequence[str]] = REQUIRED_NON_STUB_CLI_COMMANDS,
    deferred_commands: Sequence[Mapping[str, Any]] = DEFERRED_CLI_COMMANDS,
    cli_module: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    script_path = root / "scripts" / "loopplane"
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    missing: list[str] = []
    stubbed: list[str] = []

    if cli_module is None:
        try:
            cli_module = _load_cli_module(script_path, root)
        except Exception as error:  # pragma: no cover - exercised through release gate output.
            return {
                "name": "required_command_handlers_non_stub",
                "status": "fail",
                "script": _relative(script_path, root),
                "required_commands": [_command_label(command) for command in required_commands],
                "checked": [],
                "missing_required_commands": [_command_label(command) for command in required_commands],
                "stubbed_required_commands": [],
                "deferred_commands": _format_deferred_commands(deferred_commands),
                "errors": [f"unable to inspect CLI command handlers: {error}"],
            }

    try:
        parser = cli_module.build_parser()
    except Exception as error:
        return {
            "name": "required_command_handlers_non_stub",
            "status": "fail",
            "script": _relative(script_path, root),
            "required_commands": [_command_label(command) for command in required_commands],
            "checked": [],
            "missing_required_commands": [_command_label(command) for command in required_commands],
            "stubbed_required_commands": [],
            "deferred_commands": _format_deferred_commands(deferred_commands),
            "errors": [f"unable to build CLI parser: {error}"],
        }

    handlers = _collect_cli_command_handlers(parser)
    not_implemented = getattr(cli_module, "not_implemented", None)
    for command in required_commands:
        command_key = tuple(str(part) for part in command)
        label = _command_label(command_key)
        entry = handlers.get(command_key)
        if entry is None:
            missing.append(label)
            checked.append(
                {
                    "command": label,
                    "status": "fail",
                    "problem": "missing",
                    "handler": None,
                    "command_path": label,
                }
            )
            continue

        handler = entry.get("handler")
        handler_name = _handler_name(handler)
        is_stub = handler is not None and (
            handler is not_implemented or handler_name == "not_implemented"
        )
        if is_stub:
            stubbed.append(label)
        checked.append(
            {
                "command": label,
                "status": "fail" if is_stub else "pass",
                "problem": "not_implemented" if is_stub else None,
                "handler": handler_name,
                "command_path": _command_label(entry.get("command_path") or command_key),
            }
        )

    if missing:
        errors.append(f"missing required command handler(s): {', '.join(missing)}")
    if stubbed:
        errors.append(f"stubbed required command handler(s): {', '.join(stubbed)}")

    return {
        "name": "required_command_handlers_non_stub",
        "status": "pass" if not errors else "fail",
        "script": _relative(script_path, root),
        "required_commands": [_command_label(command) for command in required_commands],
        "checked": checked,
        "missing_required_commands": missing,
        "stubbed_required_commands": stubbed,
        "deferred_commands": _format_deferred_commands(deferred_commands),
        "errors": errors,
    }


def check_required_adapters_no_notimplemented(
    package_root: Path | str | None = None,
    *,
    required_adapters: Sequence[Mapping[str, Any]] = REQUIRED_NON_STUB_ADAPTERS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    module_paths = sorted(
        {
            *ADAPTER_BASE_CONTRACT_MODULES,
            *(
                str(entry.get("module") or "")
                for entry in required_adapters
                if str(entry.get("module") or "")
            ),
        }
    )
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    source_errors: list[str] = []
    missing_adapters: list[str] = []
    failed_methods: list[str] = []
    class_map: dict[str, dict[str, Any]] = {}

    for relative in module_paths:
        path = root / relative
        if not path.is_file():
            source_errors.append(f"{relative}: adapter source file is missing")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError) as error:
            source_errors.append(f"{relative}: unable to parse adapter source: {error}")
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_map[node.name] = {
                    "class": node.name,
                    "node": node,
                    "module": relative,
                    "bases": _adapter_class_base_names(node),
                }

    for entry in required_adapters:
        adapter_name = str(entry.get("adapter") or "")
        class_name = str(entry.get("class") or "")
        module = str(entry.get("module") or "")
        method_names = _required_adapter_methods(entry)
        adapter_label = adapter_name or class_name or module
        class_entry = class_map.get(class_name)
        if class_entry is None:
            missing_adapters.append(adapter_label)
            for method_name in method_names:
                checked.append(
                    {
                        "adapter": adapter_label,
                        "class": class_name,
                        "module": module,
                        "method": method_name,
                        "status": "fail",
                        "problem": "missing_class",
                        "owner_class": None,
                        "owner_module": None,
                        "line": None,
                        "problems": ["missing_class"],
                    }
                )
            continue

        for method_name in method_names:
            owner = _resolve_adapter_method_owner(class_name, method_name, class_map)
            label = f"{adapter_label}.{method_name}"
            if owner is None:
                failed_methods.append(label)
                checked.append(
                    {
                        "adapter": adapter_label,
                        "class": class_name,
                        "module": module,
                        "method": method_name,
                        "status": "fail",
                        "problem": "missing_method",
                        "owner_class": None,
                        "owner_module": None,
                        "line": None,
                        "problems": ["missing_method"],
                    }
                )
                continue

            method_node = owner["method_node"]
            problems = _adapter_method_placeholder_problems(method_node)
            owner_class = str(owner["class"])
            if owner_class == "AgentAdapter":
                if method_name == "run":
                    _append_unique(problems, "inherits_abstract_base_contract")
                elif method_name == "doctor":
                    _append_unique(problems, "inherits_default_waiting_config_doctor")
            if _is_abstractmethod(method_node):
                _append_unique(problems, "abstractmethod")
            if problems:
                failed_methods.append(label)
            checked.append(
                {
                    "adapter": adapter_label,
                    "class": class_name,
                    "module": module,
                    "method": method_name,
                    "status": "fail" if problems else "pass",
                    "problem": problems[0] if problems else None,
                    "owner_class": owner_class,
                    "owner_module": owner["module"],
                    "line": getattr(method_node, "lineno", None),
                    "problems": problems,
                }
            )

    errors.extend(source_errors)
    if missing_adapters:
        errors.append(f"missing required adapter class(es): {', '.join(missing_adapters)}")
    if failed_methods:
        errors.append(f"placeholder required adapter method(s): {', '.join(failed_methods)}")

    return {
        "name": "required_adapters_no_notimplemented",
        "status": "pass" if not errors else "fail",
        "required_adapters": [
            {
                "adapter": str(entry.get("adapter") or ""),
                "class": str(entry.get("class") or ""),
                "module": str(entry.get("module") or ""),
                "required_methods": _required_adapter_methods(entry),
            }
            for entry in required_adapters
        ],
        "checked": checked,
        "missing_required_adapters": missing_adapters,
        "failed_required_methods": failed_methods,
        "source_errors": source_errors,
        "ignored_abstract_contracts": [
            {
                "module": "runtime/adapters/base.py",
                "class": "AgentAdapter",
                "method": "run",
                "reason": "abstract base-class contract; concrete required adapters must not resolve to it",
            }
        ],
        "errors": errors,
    }


def check_recommended_cli_fixture_flows(
    package_root: Path | str | None = None,
    *,
    fixture_bin_dir: Path | str | None = None,
    flows: Sequence[Mapping[str, Any]] = RECOMMENDED_CLI_FIXTURE_FLOWS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    source_fixture_dir = (
        Path(fixture_bin_dir).expanduser().resolve()
        if fixture_bin_dir is not None
        else root / "tests" / "fixtures" / "cli_adapters" / "bin"
    )
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    missing_fixture_commands: list[str] = []

    if not source_fixture_dir.is_dir():
        errors.append(f"{_display_path(source_fixture_dir, root)}: CLI adapter fixture directory is missing")
        return {
            "name": "recommended_cli_fixture_flows",
            "schema_version": RECOMMENDED_CLI_FIXTURE_FLOW_SCHEMA_VERSION,
            "status": "fail",
            "spec_sources": [
                "LoopPlane.md 2.2 CLI flow",
                "LoopPlane.md 9.9 agent_runners.json",
                "LoopPlane.md 11 Agent Runner Adapter Contract",
                "LoopPlane.md 26 MVP Scope",
                "LoopPlane.md 28 Testing Plan",
                "LoopPlane.md 26 MVP Scope",
            ],
            "fixture_bin_dir": _display_path(source_fixture_dir, root),
            "checked": checked,
            "failed_flows": [str(flow.get("name") or flow.get("adapter") or "") for flow in flows],
            "missing_fixture_commands": [],
            "errors": errors,
        }

    with tempfile.TemporaryDirectory(prefix="loopplane-cli-fixture-flow-") as tmp:
        temp_root = Path(tmp)
        fixture_bin = temp_root / "fixture-bin"
        fixture_bin.mkdir()
        copied_commands: dict[str, Path] = {}
        for executable in sorted({str(flow.get("fixture_executable") or "") for flow in flows}):
            if not executable:
                continue
            source = source_fixture_dir / executable
            if not source.is_file():
                missing_fixture_commands.append(executable)
                continue
            target = fixture_bin / executable
            shutil.copy2(source, target)
            target.chmod(target.stat().st_mode | 0o111)
            copied_commands[executable] = target

        for flow in flows:
            name = str(flow.get("name") or flow.get("adapter") or "unnamed_cli_fixture_flow")
            executable = str(flow.get("fixture_executable") or "")
            if executable not in copied_commands:
                checked.append(
                    {
                        "name": name,
                        "adapter": str(flow.get("adapter") or ""),
                        "status": "fail",
                        "problems": ["missing_fixture_executable"],
                        "fixture_executable": executable,
                    }
                )
                continue
            checked.append(_run_recommended_cli_fixture_flow(flow, temp_root=temp_root, fixture_bin=fixture_bin))

    failed_flows = [str(entry["name"]) for entry in checked if entry.get("status") != "pass"]
    if missing_fixture_commands:
        errors.append(f"missing CLI fixture executable(s): {', '.join(sorted(missing_fixture_commands))}")
    if failed_flows:
        errors.append(f"recommended CLI fixture flow(s) failed: {', '.join(failed_flows)}")

    return {
        "name": "recommended_cli_fixture_flows",
        "schema_version": RECOMMENDED_CLI_FIXTURE_FLOW_SCHEMA_VERSION,
        "status": "pass" if not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 2.2 CLI flow",
            "LoopPlane.md 9.9 agent_runners.json",
            "LoopPlane.md 11 Agent Runner Adapter Contract",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 28 Testing Plan",
            "LoopPlane.md 26 MVP Scope",
        ],
        "fixture_bin_dir": _display_path(source_fixture_dir, root),
        "checked": checked,
        "failed_flows": failed_flows,
        "missing_fixture_commands": sorted(missing_fixture_commands),
        "errors": errors,
    }


def _run_recommended_cli_fixture_flow(
    flow: Mapping[str, Any],
    *,
    temp_root: Path,
    fixture_bin: Path,
) -> dict[str, Any]:
    from runtime.adapters.base import AdapterInput
    from runtime.adapters.claude_code_cli_adapter import ClaudeCodeCliAdapter
    from runtime.adapters.codex_cli_adapter import CodexCliAdapter
    from runtime.agent_runners import RunnerConfig

    adapter_name = str(flow.get("adapter") or "")
    name = str(flow.get("name") or adapter_name or "unnamed_cli_fixture_flow")
    command = str(flow.get("command") or "")
    role = str(flow.get("role") or "worker")
    runner_id = str(flow.get("runner_id") or f"{adapter_name}_{role}")
    task_id = str(flow.get("task_id") or "T001")
    prompt_content = f"Execute the deterministic {name} fixture task.\n"
    flow_root = temp_root / name
    prompt_path = flow_root / "prompt.md"
    scheduler_run_dir = flow_root / ".loopplane" / "runtime" / "runs" / "run_fixture"
    role_output_dir = flow_root / ".loopplane" / "results" / task_id / "runs" / "run_fixture"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_content, encoding="utf-8")
    prompt_delivery = _flow_prompt_delivery(flow)
    runner_config = RunnerConfig(
        runner_id=runner_id,
        role=role,
        adapter=adapter_name,
        command=command,
        cwd=flow_root.as_posix(),
        prompt_delivery=prompt_delivery,
        args=(),
        env={"PATH": fixture_bin.as_posix() + os.pathsep + os.environ.get("PATH", "")},
        timeout_seconds=10,
        stream_logs=True,
        permission_policy={
            "allow_project_file_edit": True,
            "allow_command_execution": True,
            "require_approval_for_risky_commands": False,
            "read_only": False,
        },
        doctor={"check_command": f"{command} --version", "requires_auth": False},
        enabled=True,
    )
    adapter_input = AdapterInput.from_runner_config(
        run_id="run_fixture",
        workflow_id="wf_release_gate_fixture",
        runner_config=runner_config,
        prompt_path=prompt_path,
        prompt_content=prompt_content,
        scheduler_run_dir=scheduler_run_dir,
        role_output_dir=role_output_dir,
        task_id=task_id,
        task_evidence_run_dir=role_output_dir,
        cwd=flow_root.as_posix(),
    )
    adapter_by_name = {
        "codex_cli": CodexCliAdapter,
        "claude_code_cli": ClaudeCodeCliAdapter,
    }
    problems: list[str] = []
    record: dict[str, Any] = {
        "name": name,
        "adapter": adapter_name,
        "runner_id": runner_id,
        "role": role,
        "task_id": task_id,
        "command": command,
        "prompt_delivery": dict(prompt_delivery),
        "status": "fail",
        "problems": problems,
    }
    adapter_class = adapter_by_name.get(adapter_name)
    if adapter_class is None:
        problems.append("unsupported_fixture_adapter")
        return record

    try:
        adapter = adapter_class()
        doctor = adapter.doctor(adapter_input)
        doctor_payload = doctor.to_dict()
        output = adapter.run(adapter_input)
    except Exception as error:  # pragma: no cover - negative tests assert serialized failure behavior.
        problems.append("fixture_flow_exception")
        record.update(
            {
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        return record

    paths = adapter_input.output_paths()
    expected_record_path = role_output_dir / str(flow.get("expected_record") or "")
    final_text = _read_text_if_file(paths.final_output_path)
    stdout_text = _read_text_if_file(paths.stdout_path)
    stderr_text = _read_text_if_file(paths.stderr_path)
    produced = {path.as_posix() for path in output.produced_files}
    required_artifact_paths = [
        role_output_dir / str(relative)
        for relative in flow.get("required_task_artifacts", ())
    ]
    required_contract_paths = [
        scheduler_run_dir / "adapter_input.json",
        paths.stdout_path,
        paths.stderr_path,
        paths.final_output_path,
        paths.adapter_result_path,
        expected_record_path,
        *required_artifact_paths,
    ]

    if doctor.status != "ok":
        problems.append("doctor_not_ok")
    if output.exit_code != 0:
        problems.append("adapter_exit_nonzero")
    if output.timed_out:
        problems.append("adapter_timed_out")
    if output.adapter != adapter_name:
        problems.append("adapter_name_mismatch")
    if output.adapter_metadata.get("external_execution") is not True:
        problems.append("external_execution_not_recorded")
    missing_contract_paths = [path.as_posix() for path in required_contract_paths if not path.is_file()]
    if missing_contract_paths:
        problems.append("missing_contract_or_task_artifacts")
    missing_produced_paths = [
        path.as_posix()
        for path in required_contract_paths
        if path.is_file() and path.as_posix() not in produced
    ]
    if missing_produced_paths:
        problems.append("produced_files_missing_required_paths")
    expected_prefix = str(flow.get("expected_final_prefix") or "")
    if expected_prefix and not final_text.startswith(expected_prefix):
        problems.append("unexpected_final_output")
    if prompt_content not in final_text:
        problems.append("prompt_missing_from_final_output")

    fixture_record = _read_json_if_file(expected_record_path)
    if not fixture_record:
        problems.append("missing_fixture_record")
    else:
        if fixture_record.get("fixture") != flow.get("expected_fixture"):
            problems.append("fixture_record_name_mismatch")
        if fixture_record.get("prompt") != prompt_content:
            problems.append("fixture_record_prompt_mismatch")
        if fixture_record.get("prompt_source") != flow.get("expected_prompt_source"):
            problems.append("fixture_record_prompt_source_mismatch")
        env_record = fixture_record.get("env")
        if not isinstance(env_record, Mapping) or env_record.get("LOOPPLANE_ROLE") != role:
            problems.append("fixture_record_role_mismatch")
        if isinstance(env_record, Mapping) and env_record.get("LOOPPLANE_TASK_ID") != task_id:
            problems.append("fixture_record_task_mismatch")

    agent_status = _read_json_if_file(role_output_dir / "agent_status.json")
    if not agent_status:
        problems.append("missing_agent_status_json")
    else:
        if agent_status.get("status") != "completed":
            problems.append("agent_status_not_completed")
        if agent_status.get("task_id") != task_id:
            problems.append("agent_status_task_mismatch")

    record.update(
        {
            "status": "pass" if not problems else "fail",
            "doctor_status": doctor.status,
            "doctor_checks": doctor_payload["checks"],
            "exit_code": output.exit_code,
            "timed_out": output.timed_out,
            "adapter_result_path": paths.adapter_result_path.as_posix(),
            "stdout_path": paths.stdout_path.as_posix(),
            "stderr_path": paths.stderr_path.as_posix(),
            "final_output_path": paths.final_output_path.as_posix(),
            "fixture_record_path": expected_record_path.as_posix(),
            "task_artifacts": [path.as_posix() for path in required_artifact_paths],
            "missing_contract_or_task_artifacts": missing_contract_paths,
            "missing_produced_paths": missing_produced_paths,
            "produced_files_count": len(output.produced_files),
            "stdout_excerpt": _text_excerpt(stdout_text),
            "stderr_excerpt": _text_excerpt(stderr_text),
            "final_output_excerpt": _text_excerpt(final_text),
            "agent_status_status": agent_status.get("status") if isinstance(agent_status, Mapping) else None,
        }
    )
    return record


def _flow_prompt_delivery(flow: Mapping[str, Any]) -> dict[str, Any]:
    raw = flow.get("prompt_delivery")
    if not isinstance(raw, Mapping):
        return {"mode": "stdin"}
    return {str(key): value for key, value in raw.items()}


def _read_json_if_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text_if_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _text_excerpt(text: str, *, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def check_workspace_registry_current_pointer_release_gate(
    package_root: Path | str | None = None,
    *,
    resolver: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    resolver_fn = resolver or _workspace_pointer_gate_default_resolver
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    problems: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="loopplane-workspace-pointer-gate-") as tmp:
            temp_root = Path(tmp)

            valid = _workspace_pointer_gate_prepare_project(temp_root, "valid")
            checked.append(
                _workspace_pointer_gate_case(
                    valid["project"],
                    root=root,
                    name="valid_pointer_registry_resolution",
                    resolver=resolver_fn,
                    expectation="resolve",
                    expected_workflow_id=valid["current_workflow_id"],
                    registry_workflow_ids=valid["registry_workflow_ids"],
                )
            )

            missing_registry = _workspace_pointer_gate_prepare_project(temp_root, "missing-registry")
            (missing_registry["project"] / ".loopplane" / "workflow_registry.json").unlink()
            checked.append(
                _workspace_pointer_gate_case(
                    missing_registry["project"],
                    root=root,
                    name="missing_workflow_registry",
                    resolver=resolver_fn,
                    expectation="reject",
                    problem_if_accepted="missing_workflow_registry_accepted",
                )
            )

            missing_current = _workspace_pointer_gate_prepare_project(temp_root, "missing-current")
            (missing_current["project"] / ".loopplane" / "current_workflow.json").unlink()
            checked.append(
                _workspace_pointer_gate_case(
                    missing_current["project"],
                    root=root,
                    name="missing_current_workflow_pointer",
                    resolver=resolver_fn,
                    expectation="reject",
                    problem_if_accepted="missing_current_workflow_pointer_accepted",
                )
            )

            malformed_current = _workspace_pointer_gate_prepare_project(temp_root, "malformed-current")
            (malformed_current["project"] / ".loopplane" / "current_workflow.json").write_text(
                "{not valid json\n",
                encoding="utf-8",
            )
            checked.append(
                _workspace_pointer_gate_case(
                    malformed_current["project"],
                    root=root,
                    name="malformed_current_workflow_pointer",
                    resolver=resolver_fn,
                    expectation="reject",
                    problem_if_accepted="malformed_current_workflow_pointer_accepted",
                )
            )

            dangling_current = _workspace_pointer_gate_prepare_project(temp_root, "dangling-current")
            dangling_current_payload = _read_json_if_file(
                dangling_current["project"] / ".loopplane" / "current_workflow.json"
            )
            dangling_current_payload["current_workflow_id"] = dangling_current["decoy_workflow_id"]
            (dangling_current["project"] / ".loopplane" / "current_workflow.json").write_text(
                json.dumps(dangling_current_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            checked.append(
                _workspace_pointer_gate_case(
                    dangling_current["project"],
                    root=root,
                    name="dangling_current_workflow_pointer",
                    resolver=resolver_fn,
                    expectation="reject",
                    problem_if_accepted="dangling_current_workflow_pointer_accepted",
                )
            )

            unregistered_scan = _workspace_pointer_gate_prepare_project(temp_root, "unregistered-scan")
            checked.append(
                _workspace_pointer_gate_case(
                    unregistered_scan["project"],
                    root=root,
                    name="unregistered_workflow_directory_scan",
                    resolver=resolver_fn,
                    expectation="reject",
                    workflow_id=unregistered_scan["decoy_workflow_id"],
                    problem_if_accepted="unregistered_workflow_directory_scan_accepted",
                )
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("workspace_registry_current_pointer_gate_exception")
        errors.append(f"workspace registry/current pointer gate raised {type(error).__name__}: {error}")

    for record in checked:
        if record.get("status") == "pass":
            continue
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"workspace registry/current pointer smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "workspace_registry_current_pointer_release_gate",
        "schema_version": WORKSPACE_REGISTRY_CURRENT_POINTER_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 8.3.1 Workflow root resolution",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _workspace_pointer_gate_default_resolver(
    project: Path,
    *,
    workflow_id: str | None = None,
) -> tuple[Any, Mapping[str, Any]]:
    from runtime.path_resolution import load_workflow_config, resolve_current_workflow_roots

    resolution = resolve_current_workflow_roots(project, workflow_id=workflow_id)
    config = load_workflow_config(project, workflow_id=workflow_id)
    return resolution, config


def _workspace_pointer_gate_prepare_project(temp_root: Path, name: str) -> dict[str, Any]:
    project = temp_root / name
    init_project(project, f"Release gate fixture for {name}.")
    workspace = _read_json_if_file(project / ".loopplane" / "workspace.json")
    registry = _read_json_if_file(project / ".loopplane" / "workflow_registry.json")
    current = _read_json_if_file(project / ".loopplane" / "current_workflow.json")
    workflows = registry.get("workflows") if isinstance(registry.get("workflows"), Sequence) else []
    registry_workflow_ids = [
        str(record.get("workflow_id"))
        for record in workflows
        if isinstance(record, Mapping) and isinstance(record.get("workflow_id"), str)
    ]
    current_workflow_id = str(current.get("current_workflow_id") or "")
    decoy_workflow_id = _workspace_pointer_gate_decoy_id(registry_workflow_ids)
    _write_workspace_pointer_gate_decoy(project, decoy_workflow_id)
    return {
        "project": project,
        "workspace_id": str(workspace.get("workspace_id") or ""),
        "current_workflow_id": current_workflow_id,
        "registry_workflow_ids": registry_workflow_ids,
        "decoy_workflow_id": decoy_workflow_id,
    }


def _workspace_pointer_gate_decoy_id(existing_ids: Sequence[str]) -> str:
    for candidate in ("wf_20260612_deadbeef", "wf_20260612_feedcafe", "wf_20260612_bad0cafe"):
        if candidate not in existing_ids:
            return candidate
    return "wf_20260612_0000feed"


def _write_workspace_pointer_gate_decoy(project: Path, workflow_id: str) -> None:
    from runtime.path_resolution import default_workflow_path_values

    workflow_root = f".loopplane/workflows/{workflow_id}"
    root = project / workflow_root
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("runtime", "read_models", "requests", "results", "planning"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": "1.5",
        "workflow_id": workflow_id,
        "created_at": "2026-06-12T00:00:00Z",
        "project_root": ".",
        "workspace_root": ".loopplane",
        "workflow_root": workflow_root,
        "workflow_config_file": f"{workflow_root}/config/workflow.json",
        **default_workflow_path_values(workflow_root=workflow_root),
    }
    (config_dir / "workflow.json").write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _workspace_pointer_gate_case(
    project: Path,
    *,
    root: Path,
    name: str,
    resolver: Any,
    expectation: str,
    expected_workflow_id: str | None = None,
    registry_workflow_ids: Sequence[str] = (),
    workflow_id: str | None = None,
    problem_if_accepted: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "project_root": _display_path(project, root),
        "expectation": expectation,
        "workflow_id_arg": workflow_id,
        "status": "fail",
        "problems": [],
    }
    case_problems: list[str] = []
    try:
        resolution, config = resolver(project, workflow_id=workflow_id)
    except Exception as error:
        record.update(
            {
                "status": "pass" if expectation == "reject" else "fail",
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        if expectation != "reject":
            case_problems.append(f"{name}_rejected")
            record["problems"] = case_problems
        return record

    resolved_workflow_id = _workflow_pointer_resolution_value(resolution, "workflow_id") or str(
        config.get("workflow_id") or ""
    )
    workflow_root = _workflow_pointer_resolution_value(resolution, "workflow_root_value") or str(
        config.get("workflow_root") or ""
    )
    workflow_config_file = _workflow_pointer_resolution_value(resolution, "workflow_config_file_value") or str(
        config.get("workflow_config_file") or ""
    )
    source = _workflow_pointer_resolution_value(resolution, "source")
    if expectation == "reject":
        case_problems.append(problem_if_accepted or f"{name}_accepted")
    else:
        if resolved_workflow_id != expected_workflow_id:
            case_problems.append(f"{name}_ignored_current_pointer")
        if str(config.get("workflow_id") or "") != expected_workflow_id:
            case_problems.append(f"{name}_loaded_wrong_workflow_config")
        if registry_workflow_ids and resolved_workflow_id not in set(registry_workflow_ids):
            case_problems.append(f"{name}_resolved_unregistered_workflow")
        if source != "v1.6_metadata":
            case_problems.append(f"{name}_bypassed_registry_pointer_metadata")
        if workflow_config_file and not (project / workflow_config_file).is_file():
            case_problems.append(f"{name}_workflow_config_file_missing")

    record.update(
        {
            "status": "pass" if not case_problems else "fail",
            "resolved_workflow_id": resolved_workflow_id,
            "resolved_workflow_root": workflow_root,
            "resolved_workflow_config_file": workflow_config_file,
            "resolution_source": source,
            "config_workflow_id": config.get("workflow_id"),
            "registry_workflow_ids": list(registry_workflow_ids),
            "problems": case_problems,
        }
    )
    return record


def _workflow_pointer_resolution_value(resolution: Any, field: str) -> str:
    value = getattr(resolution, field, "")
    return value if isinstance(value, str) else ""


def check_v16_canonical_workflow_root_release_gate(
    package_root: Path | str | None = None,
    *,
    resolver: Any | None = None,
    surface_smoke: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    resolver_fn = resolver or _v16_canonical_workflow_root_default_resolver
    surface_fn = surface_smoke or _v16_canonical_workflow_root_default_surface_smoke
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        from runtime.init_workflow import LAYOUT_CANONICAL_V16

        with tempfile.TemporaryDirectory(prefix="loopplane-v16-canonical-root-gate-") as tmp:
            project = Path(tmp) / "canonical-v16"
            init_project(
                project,
                "Release gate fixture for canonical v1.6 workflow-root mode.",
                layout=LAYOUT_CANONICAL_V16,
            )
            checked.append(
                _v16_canonical_workflow_root_gate_case(
                    project,
                    root=root,
                    resolver=resolver_fn,
                    surface_smoke=surface_fn,
                )
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("v16_canonical_workflow_root_gate_exception")
        errors.append(f"canonical v1.6 workflow-root gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"canonical v1.6 workflow-root smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "v16_canonical_workflow_root_release_gate",
        "schema_version": V16_CANONICAL_WORKFLOW_ROOT_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 6.4 Same-workspace workflow history and active workflow policy",
            "LoopPlane.md 8 Local Workflow Instance Layout",
            "LoopPlane.md 8.3.1 Workflow root resolution",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31.4 Workflow registry",
            "LoopPlane.md 31.5 Current workflow pointer",
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _v16_canonical_workflow_root_default_resolver(
    project: Path,
) -> tuple[Any, Mapping[str, Any]]:
    from runtime.path_resolution import load_workflow_config, resolve_current_workflow_roots

    resolution = resolve_current_workflow_roots(project)
    config = load_workflow_config(project)
    return resolution, config


def _v16_canonical_workflow_root_gate_case(
    project: Path,
    *,
    root: Path,
    resolver: Any,
    surface_smoke: Any,
) -> dict[str, Any]:
    metadata_paths = {
        "workspace": project / ".loopplane" / "workspace.json",
        "workflow_registry": project / ".loopplane" / "workflow_registry.json",
        "current_workflow": project / ".loopplane" / "current_workflow.json",
    }
    registry = _read_json_if_file(metadata_paths["workflow_registry"])
    current = _read_json_if_file(metadata_paths["current_workflow"])
    workflows_payload = registry.get("workflows")
    workflows = (
        list(workflows_payload)
        if isinstance(workflows_payload, Sequence) and not isinstance(workflows_payload, (str, bytes))
        else []
    )
    current_workflow_id = str(current.get("current_workflow_id") or "")
    current_record = next(
        (
            record
            for record in workflows
            if isinstance(record, Mapping)
            and str(record.get("workflow_id") or "") == current_workflow_id
        ),
        None,
    )
    case_problems: list[str] = []

    if not workflows:
        case_problems.append("workflow_registry_empty")
    if not current_workflow_id:
        case_problems.append("current_workflow_id_missing")
    if current_record is None:
        case_problems.append("current_workflow_not_registered")

    registry_workflow_root = (
        str(current_record.get("workflow_root") or "") if isinstance(current_record, Mapping) else ""
    )
    normalized_registry_root = _normal_workflow_root_value(registry_workflow_root)
    expected_workflow_root = f".loopplane/workflows/{current_workflow_id}" if current_workflow_id else ""
    if current_record is not None and normalized_registry_root != expected_workflow_root:
        case_problems.append("registry_workflow_root_not_canonical")

    resolution_payload: Any | None = None
    config_payload: Mapping[str, Any] = {}
    paths: WorkflowPaths | None = None
    try:
        resolution_payload, config_payload = resolver(project)
        paths = WorkflowPaths.from_config(project, dict(config_payload))
    except Exception as error:
        case_problems.append("canonical_workflow_resolution_failed")
        return {
            "name": "v16_canonical_registry_workflow_root_resolution",
            "status": "fail",
            "project_root": _display_path(project, root),
            "current_workflow_id": current_workflow_id,
            "registry_workflow_root": registry_workflow_root or None,
            "expected_workflow_root": expected_workflow_root,
            "error_type": type(error).__name__,
            "error": str(error),
            "problems": case_problems,
        }

    resolved_workflow_id = _workflow_pointer_resolution_value(resolution_payload, "workflow_id") or str(
        config_payload.get("workflow_id") or ""
    )
    resolved_workflow_root = _workflow_pointer_resolution_value(
        resolution_payload,
        "workflow_root_value",
    ) or str(config_payload.get("workflow_root") or "")
    resolved_workflow_config = _workflow_pointer_resolution_value(
        resolution_payload,
        "workflow_config_file_value",
    ) or str(config_payload.get("workflow_config_file") or "")
    source = _workflow_pointer_resolution_value(resolution_payload, "source")
    expected_workflow_config = f"{expected_workflow_root}/config/workflow.json"

    if resolved_workflow_id != current_workflow_id:
        case_problems.append("current_workflow_pointer_not_respected")
    if str(config_payload.get("workflow_id") or "") != current_workflow_id:
        case_problems.append("canonical_workflow_config_not_loaded")
    if source != "v1.6_metadata":
        case_problems.append("registry_current_pointer_metadata_not_used")
    if _normal_workflow_root_value(resolved_workflow_root) != expected_workflow_root:
        case_problems.append("resolved_workflow_root_not_canonical")
    if paths.workflow_root_value != expected_workflow_root:
        case_problems.append("workflow_paths_root_not_canonical")
    if resolved_workflow_config != expected_workflow_config:
        case_problems.append("canonical_workflow_config_file_not_used")

    expected_values = {
        "brief_file": f"{expected_workflow_root}/PROJECT_BRIEF.md",
        "plan_file": f"{expected_workflow_root}/PLAN.md",
        "shared_context_file": f"{expected_workflow_root}/SHARED_CONTEXT.md",
        "runtime_dir": f"{expected_workflow_root}/runtime",
        "results_dir": f"{expected_workflow_root}/results",
        "read_models_dir": f"{expected_workflow_root}/read_models",
        "requests_dir": f"{expected_workflow_root}/requests",
        "planning_dir": f"{expected_workflow_root}/planning",
        "version_control_config_file": f"{expected_workflow_root}/config/version_control.json",
    }
    path_values = {field: paths.value(field) for field in expected_values}
    for field, expected in expected_values.items():
        if path_values[field] != expected:
            case_problems.append(f"{field}_not_canonical_workflow_root")

    _write_v16_canonical_workflow_root_gate_files(project, paths)
    projection_paths = {
        "root_brief": project / "PROJECT_BRIEF.md",
        "root_plan": project / "PLAN.md",
        "root_shared_context": project / ".loopplane" / "SHARED_CONTEXT.md",
    }
    projection_hashes_before = {
        name: _file_sha256(path)
        for name, path in projection_paths.items()
        if path.is_file()
    }
    flat_runtime_paths = {
        "runtime_dir": project / ".loopplane" / "runtime",
        "read_models_dir": project / ".loopplane" / "read_models",
        "requests_dir": project / ".loopplane" / "requests",
        "results_dir": project / ".loopplane" / "results",
        "planning_dir": project / ".loopplane" / "planning",
    }
    flat_runtime_exists_before = {
        name: path.exists()
        for name, path in flat_runtime_paths.items()
    }

    surface_result: Mapping[str, Any] = {}
    try:
        raw_surface_result = surface_smoke(project)
        if isinstance(raw_surface_result, Mapping):
            surface_result = raw_surface_result
        else:
            case_problems.append("canonical_surface_smoke_returned_non_mapping")
    except Exception as error:
        surface_result = {
            "error_type": type(error).__name__,
            "error": str(error),
        }
        case_problems.append("canonical_surface_smoke_failed")

    projection_hashes_after = {
        name: _file_sha256(path)
        for name, path in projection_paths.items()
        if path.is_file()
    }
    flat_runtime_created = [
        name
        for name, path in flat_runtime_paths.items()
        if path.exists() and not flat_runtime_exists_before.get(name, False)
    ]

    if projection_hashes_after != projection_hashes_before:
        case_problems.append("root_projection_files_mutated_as_canonical_truth")
    if flat_runtime_created:
        case_problems.append("root_flat_runtime_paths_created_for_canonical_instance")
    if surface_result.get("schema_status") != "pass":
        case_problems.append("canonical_schema_validation_failed")
    if surface_result.get("preview_expected_prompt_path") != f"{expected_workflow_root}/runtime/runs/<run_id>/prompt.md":
        case_problems.append("canonical_preview_prompt_path_not_used")
    if surface_result.get("read_models_ok") is not True:
        case_problems.append("canonical_read_model_rebuild_failed")
    if surface_result.get("read_models_dir") != f"{expected_workflow_root}/read_models":
        case_problems.append("canonical_read_models_dir_not_used")
    if surface_result.get("read_model_workflow_id") != current_workflow_id:
        case_problems.append("canonical_read_models_workflow_id_mismatch")
    if surface_result.get("read_model_task_title") != "Exercise canonical v1.6 workflow-root mode":
        case_problems.append("canonical_plan_read_model_not_loaded")
    if surface_result.get("dashboard_ok") is not True:
        case_problems.append("canonical_dashboard_render_failed")
    if surface_result.get("dashboard_read_models_dir") != f"{expected_workflow_root}/read_models":
        case_problems.append("canonical_dashboard_read_models_dir_not_used")
    if surface_result.get("dashboard_workflow_id") != current_workflow_id:
        case_problems.append("canonical_dashboard_workflow_id_mismatch")

    return {
        "name": "v16_canonical_registry_workflow_root_resolution",
        "status": "pass" if not case_problems else "fail",
        "project_root": _display_path(project, root),
        "current_workflow_id": current_workflow_id,
        "registry_workflow_root": registry_workflow_root or None,
        "expected_workflow_root": expected_workflow_root,
        "resolved_workflow_id": resolved_workflow_id,
        "resolved_workflow_root": resolved_workflow_root,
        "resolved_workflow_config_file": resolved_workflow_config,
        "resolution_source": source,
        "path_values": path_values,
        "surface_result": dict(surface_result),
        "projection_hashes_before": projection_hashes_before,
        "projection_hashes_after": projection_hashes_after,
        "root_flat_runtime_paths_created": flat_runtime_created,
        "problems": case_problems,
    }


def _v16_canonical_workflow_root_default_surface_smoke(project: Path) -> Mapping[str, Any]:
    from runtime.dashboard import render_static_dashboard
    from runtime.path_resolution import WorkflowPaths, load_workflow_config
    from runtime.read_models import rebuild_read_models
    from runtime.scheduler import preview_scheduler
    from runtime.schema_validation import validate_project_schemas

    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    schema = validate_project_schemas(project)
    preview = preview_scheduler(project, write=True)
    read_models = rebuild_read_models(project)
    status_payload = _read_json_if_file(paths.read_models_dir / "workflow_status.json")
    plan_payload = _read_json_if_file(paths.read_models_dir / "plan_index.json")
    dashboard = render_static_dashboard(
        project,
        output_dir="canonical_v16_workflow_root_release_gate_dashboard",
        rebuild_read_models_first=False,
    )
    return {
        "schema_status": schema.get("status"),
        "schema_checked_files": schema.get("checked_files", []),
        "preview_expected_prompt_path": (
            preview.get("selected", {}).get("expected_prompt_path")
            if isinstance(preview.get("selected"), Mapping)
            else None
        ),
        "read_models_ok": read_models.get("ok"),
        "read_models_dir": read_models.get("read_models_dir"),
        "read_model_workflow_id": status_payload.get("workflow_id"),
        "read_model_task_title": _v16_canonical_workflow_root_task_title(plan_payload),
        "dashboard_ok": dashboard.get("ok"),
        "dashboard_read_models_dir": dashboard.get("read_models_dir"),
        "dashboard_workflow_id": dashboard.get("workflow_id"),
    }


def _v16_canonical_workflow_root_task_title(plan_payload: Mapping[str, Any]) -> str | None:
    phases = plan_payload.get("phases")
    if not isinstance(phases, Sequence) or isinstance(phases, (str, bytes)):
        return None
    for phase in phases:
        if not isinstance(phase, Mapping):
            continue
        tasks = phase.get("tasks")
        if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes)):
            continue
        for task in tasks:
            if isinstance(task, Mapping) and task.get("task_id") == "G001":
                title = task.get("title")
                return str(title) if isinstance(title, str) else None
    return None


def _write_v16_canonical_workflow_root_gate_files(project: Path, paths: WorkflowPaths) -> None:
    paths.brief_file.write_text(
        "Canonical v1.6 release-gate brief. This canonical workflow-root file is authoritative.\n",
        encoding="utf-8",
    )
    paths.shared_context_file.write_text(
        "Canonical v1.6 release-gate shared context. Runtime surfaces must resolve this through workflow_root.\n",
        encoding="utf-8",
    )
    paths.plan_file.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {paths.workflow_id}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: Canonical v1.6 Workflow-Root Release Gate

- [ ] G001: Exercise canonical v1.6 workflow-root mode
  - acceptance: Runtime preview, read models, and dashboard use the registered workflow_root.
  - evidence: {paths.value("results_dir")}/G001/
  - latest: {paths.value("results_dir")}/G001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 1
  - approval: not_required
  - deliverables: artifacts/result.txt
""",
        encoding="utf-8",
    )
    (project / "PROJECT_BRIEF.md").write_text(
        "ROOT PROJECTION DECOY: canonical gate must not read this as workflow truth.\n",
        encoding="utf-8",
    )
    (project / "PLAN.md").write_text(
        """# Root Projection Decoy

This root-level plan projection is intentionally stale for the canonical release gate.
""",
        encoding="utf-8",
    )
    (project / ".loopplane" / "SHARED_CONTEXT.md").write_text(
        "ROOT PROJECTION DECOY: canonical gate must not read this as workflow truth.\n",
        encoding="utf-8",
    )


def check_v15_flat_compatibility_release_gate(
    package_root: Path | str | None = None,
    *,
    resolver: Any | None = None,
    surface_smoke: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    resolver_fn = resolver or _v15_flat_compatibility_default_resolver
    surface_fn = surface_smoke or _v15_flat_compatibility_default_surface_smoke
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="loopplane-v15-flat-compat-gate-") as tmp:
            project = Path(tmp) / "flat-compatibility"
            init_project(
                project,
                "Release gate fixture for v1.5 flat compatibility.",
            )
            checked.append(
                _v15_flat_compatibility_gate_case(
                    project,
                    root=root,
                    resolver=resolver_fn,
                    surface_smoke=surface_fn,
                )
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("v15_flat_compatibility_gate_exception")
        errors.append(f"v1.5 flat compatibility gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"v1.5 flat compatibility smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "v15_flat_compatibility_release_gate",
        "schema_version": V15_FLAT_COMPATIBILITY_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 6 canonical v1.6 workflow directory",
            "LoopPlane.md 8.3 Configurable paths",
            "LoopPlane.md 8.3.1 Workflow root resolution",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31.8 Compatibility with v1.5 flat layout",
            "LoopPlane.md 31.8 Compatibility with v1.5 flat layout",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _v15_flat_compatibility_default_resolver(
    project: Path,
) -> tuple[Any, Mapping[str, Any]]:
    from runtime.path_resolution import load_workflow_config, resolve_current_workflow_roots

    resolution = resolve_current_workflow_roots(project)
    config = load_workflow_config(project)
    return resolution, config


def _v15_flat_compatibility_gate_case(
    project: Path,
    *,
    root: Path,
    resolver: Any,
    surface_smoke: Any,
) -> dict[str, Any]:
    metadata_paths = {
        "workspace": project / ".loopplane" / "workspace.json",
        "workflow_registry": project / ".loopplane" / "workflow_registry.json",
        "current_workflow": project / ".loopplane" / "current_workflow.json",
    }
    metadata_hashes_before = {
        name: _file_sha256(path)
        for name, path in metadata_paths.items()
        if path.is_file()
    }
    registry = _read_json_if_file(metadata_paths["workflow_registry"])
    current = _read_json_if_file(metadata_paths["current_workflow"])
    workflows_payload = registry.get("workflows")
    workflows = (
        list(workflows_payload)
        if isinstance(workflows_payload, Sequence) and not isinstance(workflows_payload, (str, bytes))
        else []
    )
    current_workflow_id = str(current.get("current_workflow_id") or "")
    current_record = next(
        (
            record
            for record in workflows
            if isinstance(record, Mapping)
            and str(record.get("workflow_id") or "") == current_workflow_id
        ),
        None,
    )
    case_problems: list[str] = []

    if not workflows:
        case_problems.append("workflow_registry_empty")
    if not current_workflow_id:
        case_problems.append("current_workflow_id_missing")
    if current_record is None:
        case_problems.append("current_workflow_not_registered")
    elif _normal_workflow_root_value(str(current_record.get("workflow_root") or "")) != ".loopplane":
        case_problems.append("registry_workflow_root_not_flat")

    resolution_payload: Any | None = None
    config_payload: Mapping[str, Any] = {}
    paths: WorkflowPaths | None = None
    try:
        resolution_payload, config_payload = resolver(project)
        paths = WorkflowPaths.from_config(project, dict(config_payload))
    except Exception as error:
        case_problems.append("flat_workflow_resolution_failed")
        metadata_hashes_after = {
            name: _file_sha256(path)
            for name, path in metadata_paths.items()
            if path.is_file()
        }
        return {
            "name": "v15_flat_registry_workflow_root_resolution",
            "status": "fail",
            "project_root": _display_path(project, root),
            "current_workflow_id": current_workflow_id,
            "registry_workflow_root": (
                current_record.get("workflow_root") if isinstance(current_record, Mapping) else None
            ),
            "error_type": type(error).__name__,
            "error": str(error),
            "metadata_hashes_before": metadata_hashes_before,
            "metadata_hashes_after": metadata_hashes_after,
            "problems": case_problems,
        }

    resolved_workflow_id = _workflow_pointer_resolution_value(resolution_payload, "workflow_id") or str(
        config_payload.get("workflow_id") or ""
    )
    resolved_workflow_root = _workflow_pointer_resolution_value(
        resolution_payload,
        "workflow_root_value",
    ) or str(config_payload.get("workflow_root") or "")
    resolved_workflow_config = _workflow_pointer_resolution_value(
        resolution_payload,
        "workflow_config_file_value",
    ) or str(config_payload.get("workflow_config_file") or "")
    source = _workflow_pointer_resolution_value(resolution_payload, "source")

    if resolved_workflow_id != current_workflow_id:
        case_problems.append("current_workflow_pointer_not_respected")
    if str(config_payload.get("workflow_id") or "") != current_workflow_id:
        case_problems.append("flat_workflow_config_not_loaded")
    if source != "v1.6_metadata":
        case_problems.append("registry_current_pointer_metadata_not_used")
    if _normal_workflow_root_value(resolved_workflow_root) != ".loopplane":
        case_problems.append("resolved_workflow_root_not_flat")
    if paths.workflow_root_value != ".loopplane":
        case_problems.append("workflow_paths_root_not_flat")
    if resolved_workflow_config != ".loopplane/config/workflow.json":
        case_problems.append("flat_workflow_config_file_not_preserved")

    expected_values = {
        "brief_file": "PROJECT_BRIEF.md",
        "plan_file": "PLAN.md",
        "shared_context_file": ".loopplane/SHARED_CONTEXT.md",
        "runtime_dir": ".loopplane/runtime",
        "results_dir": ".loopplane/results",
        "read_models_dir": ".loopplane/read_models",
        "requests_dir": ".loopplane/requests",
        "planning_dir": ".loopplane/planning",
        "version_control_config_file": ".loopplane/config/version_control.json",
    }
    path_values = {field: paths.value(field) for field in expected_values}
    for field, expected in expected_values.items():
        if path_values[field] != expected:
            case_problems.append(f"{field}_not_flat_compatible")

    surface_result: Mapping[str, Any] = {}
    try:
        raw_surface_result = surface_smoke(project)
        if isinstance(raw_surface_result, Mapping):
            surface_result = raw_surface_result
        else:
            case_problems.append("flat_surface_smoke_returned_non_mapping")
    except Exception as error:
        surface_result = {
            "error_type": type(error).__name__,
            "error": str(error),
        }
        case_problems.append("flat_surface_smoke_failed")

    metadata_hashes_after = {
        name: _file_sha256(path)
        for name, path in metadata_paths.items()
        if path.is_file()
    }
    canonical_workflow_dirs = [
        path.relative_to(project).as_posix()
        for path in sorted((project / ".loopplane" / "workflows").glob("*"))
        if path.is_dir()
    ]
    if metadata_hashes_after != metadata_hashes_before:
        case_problems.append("flat_workspace_metadata_mutated")
    if canonical_workflow_dirs:
        case_problems.append("canonical_workflow_directory_created_for_flat_instance")
    if surface_result.get("schema_status") != "pass":
        case_problems.append("flat_schema_validation_failed")
    if surface_result.get("preview_expected_prompt_path") != ".loopplane/runtime/runs/<run_id>/prompt.md":
        case_problems.append("flat_preview_prompt_path_not_used")
    if surface_result.get("read_models_ok") is not True:
        case_problems.append("flat_read_model_rebuild_failed")
    if surface_result.get("read_models_dir") != ".loopplane/read_models":
        case_problems.append("flat_read_models_dir_not_used")
    if surface_result.get("dashboard_ok") is not True:
        case_problems.append("flat_dashboard_render_failed")
    if surface_result.get("dashboard_read_models_dir") != ".loopplane/read_models":
        case_problems.append("flat_dashboard_read_models_dir_not_used")

    return {
        "name": "v15_flat_registry_workflow_root_resolution",
        "status": "pass" if not case_problems else "fail",
        "project_root": _display_path(project, root),
        "current_workflow_id": current_workflow_id,
        "registry_workflow_root": (
            current_record.get("workflow_root") if isinstance(current_record, Mapping) else None
        ),
        "resolved_workflow_id": resolved_workflow_id,
        "resolved_workflow_root": resolved_workflow_root,
        "resolved_workflow_config_file": resolved_workflow_config,
        "resolution_source": source,
        "path_values": path_values,
        "surface_result": dict(surface_result),
        "canonical_workflow_dirs": canonical_workflow_dirs,
        "metadata_hashes_before": metadata_hashes_before,
        "metadata_hashes_after": metadata_hashes_after,
        "problems": case_problems,
    }


def _v15_flat_compatibility_default_surface_smoke(project: Path) -> Mapping[str, Any]:
    from runtime.dashboard import render_static_dashboard
    from runtime.path_resolution import WorkflowPaths, load_workflow_config
    from runtime.read_models import rebuild_read_models
    from runtime.scheduler import preview_scheduler
    from runtime.schema_validation import validate_project_schemas

    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    _write_v15_flat_compatibility_gate_plan(paths)
    schema = validate_project_schemas(project)
    preview = preview_scheduler(project, write=True)
    read_models = rebuild_read_models(project)
    dashboard = render_static_dashboard(
        project,
        output_dir="flat_compatibility_release_gate_dashboard",
        rebuild_read_models_first=False,
    )
    return {
        "schema_status": schema.get("status"),
        "schema_checked_files": schema.get("checked_files", []),
        "preview_expected_prompt_path": (
            preview.get("selected", {}).get("expected_prompt_path")
            if isinstance(preview.get("selected"), Mapping)
            else None
        ),
        "read_models_ok": read_models.get("ok"),
        "read_models_dir": read_models.get("read_models_dir"),
        "dashboard_ok": dashboard.get("ok"),
        "dashboard_read_models_dir": dashboard.get("read_models_dir"),
        "dashboard_workflow_id": dashboard.get("workflow_id"),
    }


def _write_v15_flat_compatibility_gate_plan(paths: WorkflowPaths) -> None:
    paths.plan_file.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {paths.workflow_id}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: v1.5 Flat Compatibility Release Gate

- [ ] G001: Exercise flat compatibility path resolution
  - acceptance: Runtime preview keeps the v1.5 flat workflow rooted at .loopplane/.
  - evidence: {paths.value("results_dir")}/G001/
  - latest: {paths.value("results_dir")}/G001/latest.json
  - depends_on: []
  - risk: low
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 1
  - approval: not_required
  - deliverables: artifacts/result.txt
""",
        encoding="utf-8",
    )


def check_loopplane_home_authority_separation_release_gate(
    package_root: Path | str | None = None,
    *,
    resolver: Any | None = None,
    surface_smoke: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    resolver_fn = resolver or _loopplane_home_authority_default_resolver
    surface_fn = surface_smoke or _loopplane_home_authority_default_surface_smoke
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        from runtime.init_workflow import LAYOUT_CANONICAL_V16

        with tempfile.TemporaryDirectory(prefix="loopplane-home-authority-gate-") as tmp:
            temp_root = Path(tmp)
            project = temp_root / "project"
            initialized = init_project(
                project,
                "Release gate fixture for LOOPPLANE_HOME authority separation.",
                layout=LAYOUT_CANONICAL_V16,
            )
            fixture = _loopplane_home_authority_gate_prepare_fixture(temp_root, project, initialized)
            checked.append(
                _loopplane_home_authority_gate_case(
                    project,
                    root=root,
                    fixture=fixture,
                    resolver=resolver_fn,
                    surface_smoke=surface_fn,
                )
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("loopplane_home_authority_gate_exception")
        errors.append(f"LOOPPLANE_HOME authority separation gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"LOOPPLANE_HOME authority separation smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "loopplane_home_authority_separation_release_gate",
        "schema_version": LOOPPLANE_HOME_AUTHORITY_SEPARATION_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 4 Core Principles",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 32 Multi-Project Workspace Management Protocol",
            "LoopPlane.md 32 Multi-Project Workspace Management Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _loopplane_home_authority_default_resolver(project: Path) -> tuple[Any, Mapping[str, Any]]:
    from runtime.path_resolution import load_workflow_config, resolve_current_workflow_roots

    resolution = resolve_current_workflow_roots(project)
    config = load_workflow_config(project)
    return resolution, config


def _loopplane_home_authority_gate_prepare_fixture(
    temp_root: Path,
    project: Path,
    initialized: InitResult,
) -> dict[str, Any]:
    from runtime.loopplane_home import loopplane_home_layout, ensure_loopplane_home_layout
    from runtime.path_resolution import default_workflow_path_values

    home = temp_root / "loopplane-home"
    ensure_loopplane_home_layout(home)
    layout = loopplane_home_layout(home)
    local_workflow_root = f".loopplane/workflows/{initialized.workflow_id}"
    poison_workflow_id = _loopplane_home_authority_gate_decoy_id(initialized.workflow_id)
    poison_workflow_root = f".loopplane/workflows/{poison_workflow_id}"
    other_project = temp_root / "missing-project"
    poison_root = project / poison_workflow_root
    (poison_root / "config").mkdir(parents=True, exist_ok=True)
    for directory in ("runtime", "read_models", "requests", "results", "planning"):
        (poison_root / directory).mkdir(parents=True, exist_ok=True)
    poison_config = {
        "schema_version": "1.5",
        "workflow_id": poison_workflow_id,
        "created_at": "2026-06-12T00:00:00Z",
        "project_root": ".",
        "workspace_root": ".loopplane",
        "workflow_root": poison_workflow_root,
        "workflow_config_file": f"{poison_workflow_root}/config/workflow.json",
        **default_workflow_path_values(workflow_root=poison_workflow_root),
    }
    _write_json_file(poison_root / "config" / "workflow.json", poison_config)
    (poison_root / "PROJECT_BRIEF.md").write_text(
        "LOOPPLANE_HOME registry decoy brief. Project-local metadata must not select this workflow.\n",
        encoding="utf-8",
    )
    (poison_root / "PLAN.md").write_text(
        "# LOOPPLANE_HOME Registry Decoy\n\nThis plan must not become workflow truth.\n",
        encoding="utf-8",
    )

    poisoned_registry = {
        "schema_version": "1.6",
        "authority": "discovery_only",
        "generated_at": "2026-06-12T00:00:00Z",
        "workspaces": [
            {
                "workspace_id": "ws_conflicting_global_workspace",
                "name": "same project with conflicting global state",
                "project_root": project.resolve().as_posix(),
                "loopplane_dir": (project / ".loopplane").resolve().as_posix(),
                "repo_root": project.resolve().as_posix(),
                "status": "registered",
                "last_seen_at": "2026-06-12T00:00:00Z",
                "current_workflow_id": poison_workflow_id,
                "workflow_root": poison_workflow_root,
            },
            {
                "workspace_id": initialized.workspace_id,
                "name": "same id at stale project",
                "project_root": other_project.resolve().as_posix(),
                "loopplane_dir": (other_project / ".loopplane").resolve().as_posix(),
                "repo_root": other_project.resolve().as_posix(),
                "status": "registered",
                "last_seen_at": "2026-06-12T00:00:00Z",
                "current_workflow_id": "wf_20260612_badf00d0",
                "workflow_root": ".loopplane/workflows/wf_20260612_badf00d0",
            },
        ],
    }
    _write_json_file(layout.workspace_registry_file, poisoned_registry)
    _write_json_file(
        layout.agent_runners_local_file,
        {
            "schema_version": "1.6",
            "runners": {},
        },
    )
    _write_json_file(
        layout.dashboard_servers_file,
        {
            "schema_version": "1.6",
            "servers": [
                {
                    "project_root": project.resolve().as_posix(),
                    "workspace_id": "ws_conflicting_global_workspace",
                    "current_workflow_id": poison_workflow_id,
                    "selected_workflow_id": poison_workflow_id,
                    "url": "http://127.0.0.1:3766",
                }
            ],
        },
    )

    return {
        "loopplane_home": home.resolve(),
        "registry_file": layout.workspace_registry_file,
        "poison_workflow_id": poison_workflow_id,
        "poison_workflow_root": poison_workflow_root,
        "local_workspace_id": initialized.workspace_id,
        "local_workflow_id": initialized.workflow_id,
        "local_workflow_root": local_workflow_root,
        "home_files": {
            "registry": layout.workspace_registry_file,
            "agent_runners_local": layout.agent_runners_local_file,
            "dashboard_servers": layout.dashboard_servers_file,
        },
    }


def _loopplane_home_authority_gate_case(
    project: Path,
    *,
    root: Path,
    fixture: Mapping[str, Any],
    resolver: Any,
    surface_smoke: Any,
) -> dict[str, Any]:
    from runtime.workspaces import doctor_workspace, list_workspaces, load_current_workspace

    expected_workspace_id = str(fixture.get("local_workspace_id") or "")
    expected_workflow_id = str(fixture.get("local_workflow_id") or "")
    expected_workflow_root = str(fixture.get("local_workflow_root") or "")
    poison_workflow_id = str(fixture.get("poison_workflow_id") or "")
    poison_workflow_root = str(fixture.get("poison_workflow_root") or "")
    loopplane_home = Path(str(fixture.get("loopplane_home") or ""))
    metadata_paths = {
        "workspace": project / ".loopplane" / "workspace.json",
        "workflow_registry": project / ".loopplane" / "workflow_registry.json",
        "current_workflow": project / ".loopplane" / "current_workflow.json",
        "workflow_config": project / expected_workflow_root / "config" / "workflow.json",
    }
    home_files_raw = fixture.get("home_files")
    home_paths = {
        key: Path(str(value))
        for key, value in (home_files_raw.items() if isinstance(home_files_raw, Mapping) else ())
    }
    project_hashes_before = {name: _file_sha256(path) for name, path in metadata_paths.items()}
    home_hashes_before = {name: _file_sha256(path) for name, path in home_paths.items()}
    case_problems: list[str] = []
    resolution_payload: Any | None = None
    config_payload: Mapping[str, Any] = {}
    paths: WorkflowPaths | None = None
    surface_result: Mapping[str, Any] = {}
    workspace_current: Mapping[str, Any] = {}
    workspace_list: Mapping[str, Any] = {}
    workspace_doctor: Mapping[str, Any] = {}
    error_type: str | None = None
    error: str | None = None

    previous_home = os.environ.get("LOOPPLANE_HOME")
    os.environ["LOOPPLANE_HOME"] = loopplane_home.as_posix()
    try:
        try:
            resolution_payload, config_payload = resolver(project)
            paths = WorkflowPaths.from_config(project, dict(config_payload))
        except Exception as exc:
            error_type = type(exc).__name__
            error = str(exc)
            case_problems.append("project_local_resolution_failed")

        if paths is not None:
            try:
                raw_surface_result = surface_smoke(project, loopplane_home=loopplane_home)
                if isinstance(raw_surface_result, Mapping):
                    surface_result = raw_surface_result
                else:
                    case_problems.append("loopplane_home_authority_surface_smoke_returned_non_mapping")
            except Exception as exc:
                surface_result = {"error_type": type(exc).__name__, "error": str(exc)}
                case_problems.append("loopplane_home_authority_surface_smoke_failed")

        workspace_current = load_current_workspace(project)
        workspace_list = list_workspaces(loopplane_home=loopplane_home)
        workspace_doctor = doctor_workspace(project, loopplane_home=loopplane_home)
    finally:
        if previous_home is None:
            os.environ.pop("LOOPPLANE_HOME", None)
        else:
            os.environ["LOOPPLANE_HOME"] = previous_home

    project_hashes_after = {name: _file_sha256(path) for name, path in metadata_paths.items()}
    home_hashes_after = {name: _file_sha256(path) for name, path in home_paths.items()}

    resolved_workflow_id = (
        _workflow_pointer_resolution_value(resolution_payload, "workflow_id")
        if resolution_payload is not None
        else ""
    ) or str(config_payload.get("workflow_id") or "")
    resolved_workflow_root = (
        _workflow_pointer_resolution_value(resolution_payload, "workflow_root_value")
        if resolution_payload is not None
        else ""
    ) or str(config_payload.get("workflow_root") or "")
    source = (
        _workflow_pointer_resolution_value(resolution_payload, "source")
        if resolution_payload is not None
        else ""
    )
    path_values = {field: paths.value(field) for field in WORKFLOW_PATH_FIELDS} if paths is not None else {}

    if resolved_workflow_id != expected_workflow_id:
        case_problems.append("loopplane_home_registry_overrode_project_workflow")
    if _normal_workflow_root_value(resolved_workflow_root) != expected_workflow_root:
        case_problems.append("project_local_workflow_root_not_used")
    if source != "v1.6_metadata":
        case_problems.append("project_local_metadata_not_used")
    if str(config_payload.get("workflow_id") or "") != expected_workflow_id:
        case_problems.append("project_local_workflow_config_not_loaded")
    if paths is not None and paths.workflow_root_value != expected_workflow_root:
        case_problems.append("project_local_workflow_paths_not_used")

    expected_path_values = {
        "brief_file": f"{expected_workflow_root}/PROJECT_BRIEF.md",
        "plan_file": f"{expected_workflow_root}/PLAN.md",
        "shared_context_file": f"{expected_workflow_root}/SHARED_CONTEXT.md",
        "runtime_dir": f"{expected_workflow_root}/runtime",
        "results_dir": f"{expected_workflow_root}/results",
        "read_models_dir": f"{expected_workflow_root}/read_models",
        "requests_dir": f"{expected_workflow_root}/requests",
        "planning_dir": f"{expected_workflow_root}/planning",
        "version_control_config_file": f"{expected_workflow_root}/config/version_control.json",
    }
    for field, expected in expected_path_values.items():
        if path_values.get(field) != expected:
            case_problems.append(f"{field}_not_project_local")

    if surface_result.get("schema_status") != "pass":
        case_problems.append("project_local_schema_validation_failed")
    if surface_result.get("preview_expected_prompt_path") != f"{expected_workflow_root}/runtime/runs/<run_id>/prompt.md":
        case_problems.append("preview_used_loopplane_home_workflow")
    if surface_result.get("read_models_dir") != f"{expected_workflow_root}/read_models":
        case_problems.append("read_models_used_loopplane_home_workflow")
    if surface_result.get("read_model_workflow_id") != expected_workflow_id:
        case_problems.append("read_model_workflow_id_from_loopplane_home")
    if surface_result.get("dashboard_workflow_id") != expected_workflow_id:
        case_problems.append("dashboard_used_loopplane_home_workflow")
    if surface_result.get("dashboard_read_models_dir") != f"{expected_workflow_root}/read_models":
        case_problems.append("dashboard_read_models_used_loopplane_home_workflow")

    if workspace_current.get("workspace_id") != expected_workspace_id:
        case_problems.append("workspace_current_used_loopplane_home_workspace_id")
    if workspace_current.get("current_workflow_id") != expected_workflow_id:
        case_problems.append("workspace_current_used_loopplane_home_workflow")
    if workspace_current.get("workflow_root") != expected_workflow_root:
        case_problems.append("workspace_current_used_loopplane_home_workflow_root")

    if workspace_list.get("registry_authority") != "discovery_only":
        case_problems.append("global_registry_not_marked_discovery_only")
    matching_list_record = _loopplane_home_authority_matching_workspace_list_record(workspace_list, project)
    matching_health = (
        matching_list_record.get("health")
        if isinstance(matching_list_record.get("health"), Mapping)
        else {}
    )
    if not matching_list_record:
        case_problems.append("workspace_list_missing_poisoned_project_record")
    else:
        if matching_health.get("project_local_workspace_id") != expected_workspace_id:
            case_problems.append("workspace_list_did_not_report_project_local_workspace")
        if matching_health.get("project_local_current_workflow_id") != expected_workflow_id:
            case_problems.append("workspace_list_did_not_report_project_local_workflow")
        if matching_health.get("project_local_workflow_root") != expected_workflow_root:
            case_problems.append("workspace_list_did_not_report_project_local_workflow_root")

    if workspace_doctor.get("workspace_id") != expected_workspace_id:
        case_problems.append("workspace_doctor_used_loopplane_home_workspace_id")
    if workspace_doctor.get("current_workflow_id") != expected_workflow_id:
        case_problems.append("workspace_doctor_used_loopplane_home_workflow")
    doctor_issue_codes = {
        str(issue.get("code") or "")
        for issue in workspace_doctor.get("issues", [])
        if isinstance(issue, Mapping)
    }
    if "global_registry_workspace_mismatch" not in doctor_issue_codes:
        case_problems.append("workspace_doctor_did_not_warn_workspace_mismatch")
    if "global_registry_current_workflow_mismatch" not in doctor_issue_codes:
        case_problems.append("workspace_doctor_did_not_warn_workflow_mismatch")

    if project_hashes_after != project_hashes_before:
        case_problems.append("project_local_metadata_mutated")
    if home_hashes_after != home_hashes_before:
        case_problems.append("loopplane_home_state_mutated")

    return {
        "name": "project_local_authority_with_poisoned_loopplane_home",
        "status": "pass" if not case_problems else "fail",
        "project_root": _display_path(project, root),
        "loopplane_home": _display_path(loopplane_home, root),
        "expected_workspace_id": expected_workspace_id,
        "expected_workflow_id": expected_workflow_id,
        "expected_workflow_root": expected_workflow_root,
        "poison_workflow_id": poison_workflow_id,
        "poison_workflow_root": poison_workflow_root,
        "resolved_workflow_id": resolved_workflow_id,
        "resolved_workflow_root": resolved_workflow_root,
        "resolution_source": source,
        "path_values": path_values,
        "surface_result": dict(surface_result),
        "workspace_current": _loopplane_home_authority_workspace_summary(workspace_current),
        "workspace_list": _loopplane_home_authority_workspace_list_summary(workspace_list, project),
        "workspace_doctor": _loopplane_home_authority_workspace_doctor_summary(workspace_doctor),
        "project_hashes_before": project_hashes_before,
        "project_hashes_after": project_hashes_after,
        "loopplane_home_hashes_before": home_hashes_before,
        "loopplane_home_hashes_after": home_hashes_after,
        "error_type": error_type,
        "error": error,
        "problems": case_problems,
    }


def _loopplane_home_authority_default_surface_smoke(project: Path, *, loopplane_home: Path) -> Mapping[str, Any]:
    from runtime.dashboard import render_static_dashboard
    from runtime.path_resolution import WorkflowPaths, load_workflow_config
    from runtime.read_models import rebuild_read_models
    from runtime.scheduler import preview_scheduler
    from runtime.schema_validation import validate_project_schemas

    previous_home = os.environ.get("LOOPPLANE_HOME")
    os.environ["LOOPPLANE_HOME"] = loopplane_home.as_posix()
    try:
        workflow = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow)
        _write_loopplane_home_authority_gate_plan(paths)
        _accept_loopplane_home_authority_gate_plan(paths)
        schema = validate_project_schemas(project)
        preview = preview_scheduler(project, write=True)
        read_models = rebuild_read_models(project)
        status_payload = _read_json_if_file(paths.read_models_dir / "workflow_status.json")
        dashboard = render_static_dashboard(
            project,
            output_dir="loopplane_home_authority_release_gate_dashboard",
            rebuild_read_models_first=False,
        )
        return {
            "schema_status": schema.get("status"),
            "schema_checked_files": schema.get("checked_files", []),
            "preview_expected_prompt_path": (
                preview.get("selected", {}).get("expected_prompt_path")
                if isinstance(preview.get("selected"), Mapping)
                else None
            ),
            "read_models_ok": read_models.get("ok"),
            "read_models_dir": read_models.get("read_models_dir"),
            "read_model_workflow_id": status_payload.get("workflow_id"),
            "dashboard_ok": dashboard.get("ok"),
            "dashboard_read_models_dir": dashboard.get("read_models_dir"),
            "dashboard_workflow_id": dashboard.get("workflow_id"),
        }
    finally:
        if previous_home is None:
            os.environ.pop("LOOPPLANE_HOME", None)
        else:
            os.environ["LOOPPLANE_HOME"] = previous_home


def _accept_loopplane_home_authority_gate_plan(paths: WorkflowPaths) -> None:
    state_path = paths.runtime_dir / "state.json"
    state = _read_json_if_file(state_path)
    state["active_plan_sha256"] = "sha256:" + sha256(paths.plan_file.read_bytes()).hexdigest()
    state["configuration_problems"] = [
        problem
        for problem in state.get("configuration_problems", [])
        if isinstance(problem, Mapping) and problem.get("code") != "manual_plan_change_detected"
    ]
    state.pop("manual_plan_change", None)
    _write_json_file(state_path, state)


def _write_loopplane_home_authority_gate_plan(paths: WorkflowPaths) -> None:
    paths.plan_file.write_text(
        f"""# Project Plan

## Metadata

- workflow_id: {paths.workflow_id}
- plan_version: 1
- generated_from: {paths.value("brief_file")}
- active: true

## Phase P0: LOOPPLANE_HOME Authority Separation Release Gate

- [ ] T001: Exercise project-local authority with poisoned LOOPPLANE_HOME
  - acceptance: Runtime surfaces use project-local .loopplane workflow truth.
  - evidence: {paths.value("results_dir")}/T001/
  - latest: {paths.value("results_dir")}/T001/latest.json
  - depends_on: []
  - risk: medium
  - validation: file_exists: artifacts/result.txt; command_exit_code: 0
  - max_attempts: 1
  - approval: not_required
  - deliverables: artifacts/result.txt
""",
        encoding="utf-8",
    )


def _loopplane_home_authority_gate_decoy_id(local_workflow_id: str) -> str:
    for candidate in ("wf_20260612_deadbeef", "wf_20260612_feedcafe", "wf_20260612_bad0cafe"):
        if candidate != local_workflow_id:
            return candidate
    return "wf_20260612_0000feed"


def _loopplane_home_authority_matching_workspace_list_record(
    workspace_list: Mapping[str, Any],
    project: Path,
) -> Mapping[str, Any]:
    project_value = project.resolve().as_posix()
    workspaces = workspace_list.get("workspaces")
    if not isinstance(workspaces, Sequence) or isinstance(workspaces, (str, bytes)):
        return {}
    for workspace in workspaces:
        if isinstance(workspace, Mapping) and str(workspace.get("project_root") or "") == project_value:
            return workspace
    return {}


def _loopplane_home_authority_workspace_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "workspace_id": result.get("workspace_id"),
        "current_workflow_id": result.get("current_workflow_id"),
        "workflow_root": result.get("workflow_root"),
        "errors": list(result.get("errors", [])) if isinstance(result.get("errors"), Sequence) else [],
        "warnings": list(result.get("warnings", [])) if isinstance(result.get("warnings"), Sequence) else [],
    }


def _loopplane_home_authority_workspace_list_summary(
    result: Mapping[str, Any],
    project: Path,
) -> dict[str, Any]:
    matching = _loopplane_home_authority_matching_workspace_list_record(result, project)
    return {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "registry_authority": result.get("registry_authority"),
        "workspace_count": result.get("workspace_count"),
        "stale_count": result.get("stale_count"),
        "matching_project_record": dict(matching) if matching else {},
        "errors": list(result.get("errors", [])) if isinstance(result.get("errors"), Sequence) else [],
        "warnings": list(result.get("warnings", [])) if isinstance(result.get("warnings"), Sequence) else [],
    }


def _loopplane_home_authority_workspace_doctor_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    issue_codes = [
        str(issue.get("code") or "")
        for issue in result.get("issues", [])
        if isinstance(issue, Mapping)
    ]
    return {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "workspace_id": result.get("workspace_id"),
        "current_workflow_id": result.get("current_workflow_id"),
        "issue_codes": issue_codes,
        "errors": list(result.get("errors", [])) if isinstance(result.get("errors"), Sequence) else [],
        "warnings": list(result.get("warnings", [])) if isinstance(result.get("warnings"), Sequence) else [],
    }


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_migration_stale_state_exclusion_release_gate(
    package_root: Path | str | None = None,
    *,
    exporter: Any | None = None,
    importer: Any | None = None,
    archive_mutator: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    exporter_fn = exporter or _migration_stale_state_default_exporter
    importer_fn = importer or _migration_stale_state_default_importer
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="loopplane-migration-stale-state-gate-") as tmp:
            temp_root = Path(tmp)
            fixture = _migration_stale_state_prepare_fixture(temp_root)
            archives: dict[str, Path] = {}
            previous_home = os.environ.get("LOOPPLANE_HOME")
            os.environ["LOOPPLANE_HOME"] = fixture["loopplane_home"].as_posix()
            try:
                for profile in ("source", "stateful", "archive"):
                    archive_path = temp_root / f"{profile}.tar"
                    export_result = exporter_fn(fixture["project"], profile=profile, output=archive_path)
                    if archive_mutator is not None and archive_path.exists():
                        archive_mutator(archive_path, profile=profile, fixture=fixture)
                    archives[profile] = archive_path
                    checked.append(
                        _migration_stale_state_export_case(
                            profile=profile,
                            archive_path=archive_path,
                            export_result=export_result,
                            fixture=fixture,
                            root=root,
                        )
                    )

                if archives.get("stateful", Path()).is_file():
                    checked.append(
                        _migration_stale_state_import_case(
                            name="stateful_import_excludes_stale_state",
                            profile="stateful",
                            archive_path=archives["stateful"],
                            target=temp_root / "stateful-import",
                            read_only=False,
                            importer=importer_fn,
                            fixture=fixture,
                            root=root,
                        )
                    )
                if archives.get("archive", Path()).is_file():
                    checked.append(
                        _migration_stale_state_import_case(
                            name="archive_read_only_import_excludes_stale_state",
                            profile="archive",
                            archive_path=archives["archive"],
                            target=temp_root / "archive-import",
                            read_only=True,
                            importer=importer_fn,
                            fixture=fixture,
                            root=root,
                        )
                    )
            finally:
                if previous_home is None:
                    os.environ.pop("LOOPPLANE_HOME", None)
                else:
                    os.environ["LOOPPLANE_HOME"] = previous_home
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("migration_stale_state_gate_exception")
        errors.append(f"migration stale-state exclusion gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"migration stale-state exclusion smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "migration_stale_state_exclusion_release_gate",
        "schema_version": MIGRATION_STALE_STATE_EXCLUSION_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 25.7 Workspace, workflow history, and migration commands",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 33.2 Project-local and machine-local files",
            "LoopPlane.md 33.3 Source migration profile",
            "LoopPlane.md 33.4 Stateful migration profile",
            "LoopPlane.md 33.5 Archive migration profile",
            "LoopPlane.md 33.8 Migration invariants",
            "LoopPlane.md 33 Installation and Migration Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _migration_stale_state_default_exporter(project: Path, *, profile: str, output: Path) -> Mapping[str, Any]:
    from runtime.migration_export import export_project

    return export_project(project, profile=profile, output=output)


def _migration_stale_state_default_importer(
    archive_path: Path,
    *,
    target: Path,
    read_only: bool = False,
) -> Mapping[str, Any]:
    from runtime.migration_import import import_project_archive

    return import_project_archive(archive_path, target=target, read_only=read_only)


def _migration_stale_state_prepare_fixture(temp_root: Path) -> dict[str, Any]:
    from runtime.init_workflow import LAYOUT_CANONICAL_V16
    from runtime.path_resolution import WorkflowPaths, load_workflow_config

    project = temp_root / "source-project"
    init_project(
        project,
        "Release gate fixture for migration stale-state exclusion.",
        layout=LAYOUT_CANONICAL_V16,
    )
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    workflow_root = paths.workflow_root_value.rstrip("/")
    token = "loopplane-release-gate-dashboard-token"
    runner_secret = "loopplane-release-gate-runner-secret"
    local_exec = "/opt/loopplane-local/bin/codex"
    local_prompt = "/tmp/loopplane-local/prompt.md"
    local_project = "/tmp/loopplane-local/project"

    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / ".env").write_text(f"TOKEN={runner_secret}\n", encoding="utf-8")

    agent_config_path = paths.config_file("agent_runners.json")
    agent_config = _read_json_if_file(agent_config_path)
    runners = agent_config.setdefault("runners", {})
    worker = runners.setdefault("worker", {})
    if isinstance(worker, dict):
        worker["command"] = f"{local_exec} --config /tmp/loopplane-local/codex.toml --token {runner_secret}"
        worker["cwd"] = local_project
        worker["env"] = {"OPENAI_API_KEY": runner_secret, "DASHBOARD_TOKEN": token}
        worker["api_key"] = runner_secret
        worker["dashboard_token"] = token
        doctor = worker.setdefault("doctor", {})
        if isinstance(doctor, dict):
            doctor["check_command"] = f"{local_exec} --version"
            doctor["auth_check_command"] = f"{local_exec} auth status --token={runner_secret}"
    _write_json_file(agent_config_path, agent_config)

    _write_json_file(project / ".loopplane" / "config" / "local" / "agent_runners.local.json", {"secret": runner_secret})
    _write_json_file(paths.runtime_dir / "lock" / "scheduler_instance_lock" / "owner.json", {"pid": 12345})
    _write_json_file(paths.runtime_dir / "active_run_leases" / "run_001.json", {"pid": 12345})
    (paths.runtime_dir / "dashboard_token").write_text(token + "\n", encoding="utf-8")
    _write_json_file(paths.runtime_dir / "dashboard_server.json", {"pid": 12345, "token": token})
    _write_json_file(paths.runtime_dir / "background_jobs.json", {"jobs": [{"pid": 45678, "command": local_exec}]})
    _write_json_file(paths.runtime_dir / "supervisor.json", {"pid": 45678, "process_handle": {"pid": 45678}})
    (paths.runtime_dir / "supervisor").mkdir()
    (paths.runtime_dir / "supervisor" / "supervisor_stdout.log").write_text("supervisor process log\n", encoding="utf-8")
    _write_json_file(paths.runtime_dir / "runs" / "run_001" / "run_metadata.json", {"adapter_pid": 45678})
    _write_json_file(paths.read_models_dir / "workflow_status.json", {"derived": True, "token": token})

    (paths.planning_dir / "runs" / "plan_001").mkdir(parents=True)
    _write_json_file(paths.planning_dir / "runs" / "plan_001" / "plan_result.json", {"status": "planned"})
    (paths.requests_dir / "control_requests.jsonl").write_text(
        json.dumps({"request_id": "req_001", "action": "pause", "token": token}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    event = _migration_stale_state_event_record(
        {
            "schema_version": "1.6",
            "sequence": 1,
            "event_id": "evt_migration_gate",
            "event_type": "task_completed",
            "timestamp": "2026-06-12T00:00:01Z",
            "data": {
                "adapter_pid": 45678,
                "process_handle": {"pid": 45678},
                "token": token,
                "command": f"{local_exec} exec {local_prompt}",
            },
        }
    )
    (paths.runtime_dir / "events" / "events_000001.jsonl").write_text(
        json.dumps(event, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_json_file(
        paths.runtime_dir / "snapshots" / "snapshot_000001.json",
        {
            "schema_version": "1.5",
            "snapshot_id": "snapshot_000001",
            "workflow_id": paths.workflow_id,
            "created_at": "2026-06-12T00:00:00Z",
            "events_through_sequence": 1,
            "state": {"status": "running", "pid": 45678, "dashboard_token": token},
        },
    )
    _write_json_file(
        paths.runtime_dir / "failure_registry.json",
        {"schema_version": "1.5", "workflow_id": paths.workflow_id, "failures": [{"failure_id": "F001", "pid": 45678}]},
    )
    (paths.runtime_dir / "git_checkpoints.jsonl").write_text(
        json.dumps({"checkpoint_id": "gitcp_migration_gate", "ref": "refs/loopplane/example"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_json_file(
        paths.runtime_dir / "evidence_manifest.json",
        {"schema_version": "1.5", "workflow_id": paths.workflow_id, "tasks": {}},
    )
    _write_json_file(
        paths.runtime_dir / "final_verification_report.json",
        {"schema_version": "1.5", "workflow_id": paths.workflow_id, "status": "pass", "checks": []},
    )

    run_dir = paths.results_dir / "T001" / "runs" / "run_001"
    (run_dir / "logs").mkdir(parents=True)
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "report.md").write_text("# Migration gate report\n", encoding="utf-8")
    _write_json_file(
        run_dir / "validation.json",
        {
            "schema_version": "1.6",
            "run_id": "run_001",
            "primary_task_id": "T001",
            "status": "pass",
            "verdict": "accepted",
        },
    )
    _write_json_file(run_dir / "node_summary.json", {"task_id": "T001"})
    _write_json_file(paths.results_dir / "T001" / "latest.json", {"run_id": "run_001"})
    _write_json_file(
        run_dir / "agent_status.json",
        {
            "schema_version": "1.6",
            "status": "completed",
            "next_prompt_ready": True,
            "background_pids": [45678],
            "background": {
                "pids": [45678],
                "commands": [f"{local_exec} --token {runner_secret}"],
                "wake_next_agent_when": "pid exits",
            },
        },
    )
    _write_json_file(
        run_dir / "adapter_result.json",
        {
            "command": f"{local_exec} exec {local_prompt} --token {runner_secret}",
            "cwd": local_project,
            "stdout_path": str(run_dir / "logs" / "stdout.log"),
            "stderr_path": str(run_dir / "logs" / "stderr.log"),
            "adapter_metadata": {
                "process_handle": {"pid": 45678},
                "runner_resource_lock": {"pid": 45678, "lock_path": "/home/user/.loopplane/locks/runner.lock"},
                "api_token": token,
            },
        },
    )
    (run_dir / "commands.sh").write_text(f"{local_exec} exec {local_prompt} --token {runner_secret}\n", encoding="utf-8")
    (run_dir / "logs" / "stdout.log").write_text("runtime log without secrets\n", encoding="utf-8")
    (run_dir / "logs" / "stderr.log").write_text("runtime stderr without secrets\n", encoding="utf-8")
    (run_dir / "artifacts" / "result.txt").write_text("artifact\n", encoding="utf-8")

    loopplane_home = temp_root / "loopplane-home"
    _write_json_file(loopplane_home / "runners" / "agent_runners.local.json", {"secret": runner_secret})
    _write_json_file(loopplane_home / "dashboard" / "servers.json", {"servers": [{"token": token, "pid": 45678}]})
    _write_json_file(loopplane_home / "locks" / "runner.lock", {"pid": 45678})

    return {
        "project": project,
        "paths": paths,
        "workflow_root": workflow_root,
        "loopplane_home": loopplane_home,
        "forbidden_values": (token, runner_secret, local_exec, local_prompt, local_project, loopplane_home.as_posix()),
    }


def _migration_stale_state_export_case(
    *,
    profile: str,
    archive_path: Path,
    export_result: Mapping[str, Any],
    fixture: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    from runtime.migration_export import EXPORT_MANIFEST_NAME, list_export_archive_members, read_export_archive_manifest

    case_problems: list[str] = []
    members: set[str] = set()
    manifest: Mapping[str, Any] = {}
    if export_result.get("ok") is not True:
        case_problems.append(f"{profile}_export_failed")
    if not archive_path.is_file():
        case_problems.append(f"{profile}_archive_missing")
    else:
        try:
            members = set(list_export_archive_members(archive_path))
            manifest = read_export_archive_manifest(archive_path)
        except Exception as error:
            case_problems.append(f"{profile}_archive_unreadable:{type(error).__name__}")
            manifest = {"error": str(error)}

    workflow_root = str(fixture["workflow_root"])
    forbidden_members = _migration_stale_state_forbidden_members(workflow_root)
    preserved_forbidden = sorted(
        member
        for member in members
        if any(member == forbidden or member.startswith(f"{forbidden.rstrip('/')}/") for forbidden in forbidden_members)
    )
    if preserved_forbidden:
        case_problems.append(f"{profile}_export_preserved_stale_members")

    missing_expected = sorted(_migration_stale_state_expected_members(profile, workflow_root) - members)
    if missing_expected:
        case_problems.append(f"{profile}_export_missing_portable_members")

    content_findings: list[dict[str, Any]] = []
    if archive_path.is_file():
        content_findings = _migration_archive_stale_content_findings(
            archive_path,
            forbidden_values=fixture["forbidden_values"],
        )
        if content_findings:
            case_problems.append(f"{profile}_export_preserved_stale_payload")

    agent_runner: Mapping[str, Any] = {}
    agent_runner_member = f"{workflow_root}/config/agent_runners.json"
    if agent_runner_member in members:
        try:
            config = json.loads(_migration_archive_text(archive_path, agent_runner_member))
            runners = config.get("runners") if isinstance(config, Mapping) else {}
            agent_runner = runners.get("worker", {}) if isinstance(runners, Mapping) else {}
        except (OSError, tarfile.TarError, json.JSONDecodeError) as error:
            case_problems.append(f"{profile}_agent_runner_config_unreadable:{type(error).__name__}")
    if agent_runner:
        if str(agent_runner.get("command") or "").startswith("/"):
            case_problems.append(f"{profile}_runner_command_not_portable")
        if "loopplane-release-gate-runner-secret" in json.dumps(agent_runner, sort_keys=True):
            case_problems.append(f"{profile}_runner_secret_not_redacted")
        if str(agent_runner.get("cwd") or "") != "{{project_root}}":
            case_problems.append(f"{profile}_runner_cwd_not_project_relative")
        if agent_runner.get("env") not in ({}, None):
            case_problems.append(f"{profile}_runner_env_preserved")

    excluded = {
        str(record.get("path") or ""): str(record.get("reason") or "")
        for record in manifest.get("excluded_paths", [])
        if isinstance(record, Mapping)
    }
    missing_exclusions = [
        path
        for path in _migration_stale_state_required_excluded_paths(workflow_root)
        if path not in excluded and any(path == forbidden or path.startswith(f"{forbidden.rstrip('/')}/") for forbidden in forbidden_members)
    ]
    if missing_exclusions:
        case_problems.append(f"{profile}_manifest_missing_stale_exclusions")

    profile_metadata_key = {
        "source": "source_profile",
        "stateful": "stateful_profile",
        "archive": "archive_profile",
    }[profile]
    profile_metadata = manifest.get(profile_metadata_key)
    profile_excludes = profile_metadata.get("excludes", []) if isinstance(profile_metadata, Mapping) else []
    if not _migration_stale_state_profile_excludes_are_documented(profile_excludes):
        case_problems.append(f"{profile}_manifest_missing_stale_state_exclusion_policy")

    return {
        "name": f"{profile}_export_stale_state_exclusion",
        "status": "pass" if not case_problems else "fail",
        "profile": profile,
        "project_root": _display_path(Path(str(fixture["project"])), root),
        "archive": _display_path(archive_path, root),
        "export_status": export_result.get("status"),
        "manifest_profile": manifest.get("profile"),
        "member_count": len(members),
        "required_missing_members": missing_expected,
        "preserved_forbidden_members": preserved_forbidden,
        "content_findings": content_findings[:20],
        "manifest_missing_exclusions": missing_exclusions,
        "sanitized_agent_runner": dict(agent_runner) if isinstance(agent_runner, Mapping) else {},
        "problems": case_problems,
    }


def _migration_stale_state_import_case(
    *,
    name: str,
    profile: str,
    archive_path: Path,
    target: Path,
    read_only: bool,
    importer: Any,
    fixture: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    case_problems: list[str] = []
    import_result = importer(archive_path, target=target, read_only=read_only)
    if import_result.get("ok") is not True:
        case_problems.append(f"{profile}_import_failed")

    workflow_root = str(fixture["workflow_root"])
    forbidden_existing = sorted(
        relative
        for relative in _migration_stale_state_import_forbidden_members(workflow_root)
        if (target / relative).exists()
    )
    if forbidden_existing:
        case_problems.append(f"{profile}_import_materialized_stale_members")

    content_findings: list[dict[str, Any]] = []
    if target.exists():
        content_findings = _migration_tree_stale_content_findings(
            target,
            forbidden_values=fixture["forbidden_values"],
        )
        if content_findings:
            case_problems.append(f"{profile}_import_preserved_stale_payload")

    runtime_state = _read_json_if_file(target / workflow_root / "runtime" / "state.json")
    background_jobs = _read_json_if_file(target / workflow_root / "runtime" / "background_jobs.json")
    agent_config = _read_json_if_file(target / workflow_root / "config" / "agent_runners.json")
    runners = agent_config.get("runners") if isinstance(agent_config.get("runners"), Mapping) else {}
    agent_runner = runners.get("worker", {}) if isinstance(runners, Mapping) else {}

    expected_state = "read_only_imported" if read_only else "waiting_config"
    if runtime_state.get("status") != expected_state:
        case_problems.append(f"{profile}_import_runtime_state_not_regenerated")
    if background_jobs.get("jobs") != []:
        case_problems.append(f"{profile}_import_background_jobs_not_reset")
    if isinstance(agent_runner, Mapping):
        if str(agent_runner.get("command") or "").startswith("/"):
            case_problems.append(f"{profile}_import_runner_command_not_portable")
        if agent_runner.get("env") not in ({}, None):
            case_problems.append(f"{profile}_import_runner_env_preserved")

    return {
        "name": name,
        "status": "pass" if not case_problems else "fail",
        "profile": profile,
        "archive": _display_path(archive_path, root),
        "target": _display_path(target, root),
        "read_only": read_only,
        "import_status": import_result.get("status"),
        "import_errors": list(import_result.get("errors", [])) if isinstance(import_result.get("errors"), Sequence) else [],
        "workflow_id": import_result.get("workflow_id"),
        "runtime_state_status": runtime_state.get("status"),
        "background_jobs": background_jobs.get("jobs"),
        "forbidden_existing_paths": forbidden_existing,
        "content_findings": content_findings[:20],
        "sanitized_agent_runner": dict(agent_runner) if isinstance(agent_runner, Mapping) else {},
        "problems": case_problems,
    }


def _migration_stale_state_expected_members(profile: str, workflow_root: str) -> set[str]:
    required = {
        "PROJECT_BRIEF.md",
        "PLAN.md",
        ".loopplane/workspace.json",
        ".loopplane/workflow_registry.json",
        ".loopplane/current_workflow.json",
        f"{workflow_root}/config/workflow.json",
        f"{workflow_root}/config/agent_runners.json",
        f"{workflow_root}/runtime/events/events_000001.jsonl",
        f"{workflow_root}/runtime/git_checkpoints.jsonl",
        f"{workflow_root}/runtime/evidence_manifest.json",
        f"{workflow_root}/runtime/final_verification_report.json",
        f"{workflow_root}/results/T001/runs/run_001/report.md",
        f"{workflow_root}/results/T001/runs/run_001/validation.json",
        f"{workflow_root}/results/T001/runs/run_001/node_summary.json",
        f"{workflow_root}/results/T001/latest.json",
    }
    if profile in {"stateful", "archive"}:
        required.update(
            {
                f"{workflow_root}/planning/runs/plan_001/plan_result.json",
                f"{workflow_root}/requests/control_requests.jsonl",
                f"{workflow_root}/runtime/snapshots/snapshot_000001.json",
                f"{workflow_root}/runtime/failure_registry.json",
                f"{workflow_root}/results/T001/runs/run_001/agent_status.json",
                f"{workflow_root}/results/T001/runs/run_001/adapter_result.json",
                f"{workflow_root}/results/T001/runs/run_001/commands.sh",
            }
        )
    return required


def _migration_stale_state_forbidden_members(workflow_root: str) -> set[str]:
    return {
        ".env",
        ".loopplane/config/local",
        f"{workflow_root}/runtime/lock",
        f"{workflow_root}/runtime/locks",
        f"{workflow_root}/runtime/active_run_leases",
        f"{workflow_root}/runtime/dashboard_token",
        f"{workflow_root}/runtime/dashboard_server.json",
        f"{workflow_root}/runtime/background_jobs.json",
        f"{workflow_root}/runtime/supervisor.json",
        f"{workflow_root}/runtime/supervisor",
        f"{workflow_root}/runtime/runs",
        f"{workflow_root}/read_models",
    }


def _migration_stale_state_required_excluded_paths(workflow_root: str) -> set[str]:
    return {
        ".env",
        ".loopplane/config/local/agent_runners.local.json",
        f"{workflow_root}/runtime/lock/scheduler_instance_lock/owner.json",
        f"{workflow_root}/runtime/active_run_leases/run_001.json",
        f"{workflow_root}/runtime/dashboard_token",
        f"{workflow_root}/runtime/dashboard_server.json",
        f"{workflow_root}/runtime/background_jobs.json",
        f"{workflow_root}/runtime/supervisor.json",
        f"{workflow_root}/runtime/supervisor/supervisor_stdout.log",
        f"{workflow_root}/runtime/runs/run_001/run_metadata.json",
        f"{workflow_root}/read_models/workflow_status.json",
    }


def _migration_stale_state_import_forbidden_members(workflow_root: str) -> set[str]:
    forbidden = set(_migration_stale_state_forbidden_members(workflow_root))
    forbidden.discard(f"{workflow_root}/runtime/background_jobs.json")
    return forbidden


def _migration_stale_state_profile_excludes_are_documented(values: Any) -> bool:
    text = " ".join(str(value).lower() for value in values if isinstance(value, str))
    required = ("lock", "lease", "pid", "dashboard", "secret", "loopplane_home")
    return all(fragment in text for fragment in required)


def _migration_archive_stale_content_findings(
    archive_path: Path,
    *,
    forbidden_values: Sequence[str],
) -> list[dict[str, Any]]:
    from runtime.migration_export import EXPORT_MANIFEST_NAME

    findings: list[dict[str, Any]] = []
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name == EXPORT_MANIFEST_NAME:
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            data = extracted.read()
            text = data.decode("utf-8", "ignore")
            findings.extend(_migration_text_stale_findings(member.name, text, forbidden_values=forbidden_values))
            if member.name.endswith(".json"):
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                findings.extend(_migration_json_stale_findings(payload, path=member.name, forbidden_values=forbidden_values))
            elif member.name.endswith(".jsonl"):
                for index, line in enumerate(text.splitlines(), start=1):
                    if not line.strip():
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    findings.extend(
                        _migration_json_stale_findings(
                            payload,
                            path=f"{member.name}:{index}",
                            forbidden_values=forbidden_values,
                        )
                    )
    return findings


def _migration_tree_stale_content_findings(
    target: Path,
    *,
    forbidden_values: Sequence[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in sorted(item for item in target.rglob("*") if item.is_file()):
        relative = path.relative_to(target).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(_migration_text_stale_findings(relative, text, forbidden_values=forbidden_values))
        if path.suffix == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            findings.extend(_migration_json_stale_findings(payload, path=relative, forbidden_values=forbidden_values))
        elif path.suffix == ".jsonl":
            for index, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                findings.extend(
                    _migration_json_stale_findings(
                        payload,
                        path=f"{relative}:{index}",
                        forbidden_values=forbidden_values,
                    )
                )
    return findings


def _migration_text_stale_findings(
    path: str,
    text: str,
    *,
    forbidden_values: Sequence[str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for value in forbidden_values:
        if value and value in text:
            findings.append({"path": path, "problem": "forbidden_machine_local_value", "value": value})
    return findings


def _migration_json_stale_findings(
    value: Any,
    *,
    path: str,
    forbidden_values: Sequence[str],
    key_path: str = "",
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{key_path}.{key}" if key_path else key
            normalized = key.lower().replace("-", "_")
            if _migration_stale_state_key_is_process_state(normalized):
                findings.append({"path": path, "problem": "stale_process_key", "key": child_path})
                continue
            if _migration_stale_state_key_is_secret(normalized) and isinstance(child, str):
                if not (
                    child.startswith("<redacted-for-")
                    or child == "<redacted-local-secret>"
                    or child == "<redacted-local-path>"
                ):
                    findings.append({"path": path, "problem": "unredacted_secret_value", "key": child_path})
            findings.extend(
                _migration_json_stale_findings(
                    child,
                    path=path,
                    forbidden_values=forbidden_values,
                    key_path=child_path,
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(
                _migration_json_stale_findings(
                    child,
                    path=path,
                    forbidden_values=forbidden_values,
                    key_path=f"{key_path}[{index}]",
                )
            )
    elif isinstance(value, str):
        for forbidden in forbidden_values:
            if forbidden and forbidden in value:
                findings.append({"path": path, "problem": "forbidden_machine_local_value", "key": key_path, "value": forbidden})
    return findings


def _migration_stale_state_key_is_process_state(normalized_key: str) -> bool:
    process_keys = {
        "pid",
        "pids",
        "adapter_pid",
        "background",
        "background_jobs",
        "background_pids",
        "lock_path",
        "lock_paths",
        "process_handle",
        "process_handles",
        "runner_resource_lock",
        "runner_resource_locks",
        "supervisor_pid",
        "wake_next_agent_when",
    }
    return normalized_key in process_keys or normalized_key.endswith("_pid") or normalized_key.endswith("_pids")


def _migration_stale_state_key_is_secret(normalized_key: str) -> bool:
    return any(
        fragment in normalized_key
        for fragment in ("api_key", "apikey", "secret", "password", "credential", "private_key", "token")
    )


def _migration_archive_text(archive_path: Path, member_name: str) -> str:
    with tarfile.open(archive_path, "r:*") as archive:
        extracted = archive.extractfile(member_name)
        if extracted is None:
            raise KeyError(member_name)
        return extracted.read().decode("utf-8")


def _migration_stale_state_event_record(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    payload.pop("event_hash", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["event_hash"] = "sha256:" + sha256(encoded).hexdigest()
    return payload


def _normal_workflow_root_value(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    return normalized or "."


def check_dashboard_history_switching_release_gate(
    package_root: Path | str | None = None,
    *,
    render_dashboard: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    checked: list[dict[str, Any]] = []
    errors: list[str] = []
    problems: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="loopplane-dashboard-history-gate-") as tmp:
            temp_root = Path(tmp)
            project = temp_root / "project"
            init_project(project, "Release gate fixture for same-workspace dashboard history switching.")
            current_workflow = _read_json_if_file(project / ".loopplane" / "config" / "workflow.json")
            current_workflow_id = str(current_workflow.get("workflow_id") or "wf_20260612_00000000")
            selected_workflow_id = "wf_20260612_00000017"
            if selected_workflow_id == current_workflow_id:
                selected_workflow_id = "wf_20260612_00000018"
            workspace = _read_json_if_file(project / ".loopplane" / "workspace.json")
            workspace_id = str(workspace.get("workspace_id") or "ws_dashboard_history_release_gate")

            _write_dashboard_history_gate_read_models(
                project,
                workflow_id=selected_workflow_id,
                workflow_root=project / ".loopplane" / "workflows" / selected_workflow_id,
                status="archived_view",
                title="historical release-gate workflow",
            )
            _write_dashboard_history_gate_read_models(
                project,
                workflow_id=current_workflow_id,
                workflow_root=project / ".loopplane",
                status="active",
                title="current release-gate workflow",
            )

            registry_path = project / ".loopplane" / "workflow_registry.json"
            current_path = project / ".loopplane" / "current_workflow.json"
            workspace_path = project / ".loopplane" / "workspace.json"
            registry = {
                "schema_version": "1.6",
                "workspace_id": workspace_id,
                "generated_at": "2026-06-12T00:00:00Z",
                "workflows": [
                    {
                        "workflow_id": current_workflow_id,
                        "name": "current release-gate workflow",
                        "status": "active",
                        "workflow_root": ".loopplane",
                        "plan_file": "PLAN.md",
                        "read_models_dir": ".loopplane/read_models",
                        "runtime_dir": ".loopplane/runtime",
                        "requests_dir": ".loopplane/requests",
                        "read_only": False,
                        "archived": False,
                        "summary": {
                            "one_line": "Current release-gate workflow.",
                            "tasks_total": 1,
                            "tasks_completed": 1,
                            "tasks_blocked": 0,
                        },
                    },
                    {
                        "workflow_id": selected_workflow_id,
                        "name": "historical release-gate workflow",
                        "status": "archived",
                        "workflow_root": f".loopplane/workflows/{selected_workflow_id}",
                        "plan_file": f".loopplane/workflows/{selected_workflow_id}/PLAN.md",
                        "read_models_dir": f".loopplane/workflows/{selected_workflow_id}/read_models",
                        "runtime_dir": f".loopplane/workflows/{selected_workflow_id}/runtime",
                        "requests_dir": f".loopplane/workflows/{selected_workflow_id}/requests",
                        "read_only": True,
                        "archived": True,
                        "summary": {
                            "one_line": "Historical release-gate workflow.",
                            "tasks_total": 1,
                            "tasks_completed": 0,
                            "tasks_blocked": 0,
                        },
                    },
                ],
            }
            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.6",
                        "workspace_id": workspace_id,
                        "current_workflow_id": current_workflow_id,
                        "selection_reason": "release_gate_current_fixture",
                        "updated_at": "2026-06-12T00:00:00Z",
                        "updated_by": "release_gate_fixture",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            workspace["schema_version"] = "1.6"
            workspace["workspace_id"] = workspace_id
            workspace["current_workflow_id"] = current_workflow_id
            workspace_path.write_text(json.dumps(workspace, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            metadata_paths = {
                "workspace": workspace_path,
                "workflow_registry": registry_path,
                "current_workflow": current_path,
            }
            hashes_before = {name: _file_sha256(path) for name, path in metadata_paths.items()}
            renderer = render_dashboard or _default_dashboard_history_gate_renderer
            output_dir = project / "dashboard_history_gate"
            result = renderer(
                project,
                output_dir=output_dir,
                rebuild_read_models_first=False,
                workflow_id=selected_workflow_id,
            )
            hashes_after = {name: _file_sha256(path) for name, path in metadata_paths.items()}
            result_payload = dict(result) if isinstance(result, Mapping) else {}
            index_file = project / str(result_payload.get("index_file") or "")
            embedded_payload = _dashboard_history_gate_embedded_payload(index_file)
            copied_status = _read_json_if_file(output_dir / "read_models" / "workflow_status.json")
            workflow_ids = [
                str(record.get("workflow_id") or "")
                for record in embedded_payload.get("workflows", [])
                if isinstance(record, Mapping)
            ]
            selected_read_models_dir = f".loopplane/workflows/{selected_workflow_id}/read_models"
            workspace_payload = embedded_payload.get("workspace") if isinstance(embedded_payload.get("workspace"), Mapping) else {}
            read_models_payload = (
                embedded_payload.get("read_models")
                if isinstance(embedded_payload.get("read_models"), Mapping)
                else {}
            )
            status_payload = (
                read_models_payload.get("workflow_status.json")
                if isinstance(read_models_payload.get("workflow_status.json"), Mapping)
                else {}
            )
            planning_controls = (
                embedded_payload.get("planning_controls")
                if isinstance(embedded_payload.get("planning_controls"), Mapping)
                else {}
            )
            execution_controls = (
                embedded_payload.get("execution_controls")
                if isinstance(embedded_payload.get("execution_controls"), Mapping)
                else {}
            )
            if result_payload.get("ok") is not True:
                problems.append("dashboard_render_failed")
            if result_payload.get("workflow_id") != selected_workflow_id:
                problems.append("selected_workflow_not_returned")
            if result_payload.get("read_models_dir") != selected_read_models_dir:
                problems.append("selected_workflow_read_models_not_used")
            if hashes_after != hashes_before:
                problems.append("workspace_metadata_mutated")
            if not embedded_payload:
                problems.append("embedded_dashboard_payload_missing")
            if embedded_payload and embedded_payload.get("workflow_id") != selected_workflow_id:
                problems.append("embedded_payload_selected_wrong_workflow")
            if workspace_payload.get("current_workflow_id") != current_workflow_id:
                problems.append("current_workflow_pointer_not_preserved_in_payload")
            if workspace_payload.get("selected_workflow_id") != selected_workflow_id:
                problems.append("workspace_payload_missing_selected_workflow")
            if status_payload.get("workflow_id") != selected_workflow_id:
                problems.append("selected_workflow_status_not_loaded")
            if copied_status.get("workflow_id") != selected_workflow_id:
                problems.append("static_read_model_copy_not_selected_workflow")
            if set(workflow_ids) != {current_workflow_id, selected_workflow_id}:
                problems.append("workspace_selector_does_not_list_same_workspace_histories")
            if planning_controls.get("mutation_allowed") is not False:
                problems.append("archived_read_only_planning_controls_not_blocked")
            if execution_controls.get("mutation_allowed") is not False:
                problems.append("archived_read_only_execution_controls_not_blocked")

            checked.append(
                {
                    "name": "static_dashboard_same_workspace_history_switching",
                    "status": "pass" if not problems else "fail",
                    "project_root": _display_path(project, root),
                    "current_workflow_id": current_workflow_id,
                    "selected_workflow_id": selected_workflow_id,
                    "result_status": result_payload.get("status"),
                    "result_workflow_id": result_payload.get("workflow_id"),
                    "result_read_models_dir": result_payload.get("read_models_dir"),
                    "embedded_workflow_id": embedded_payload.get("workflow_id"),
                    "embedded_current_workflow_id": workspace_payload.get("current_workflow_id"),
                    "embedded_selected_workflow_id": workspace_payload.get("selected_workflow_id"),
                    "embedded_workflow_ids": workflow_ids,
                    "metadata_hashes_before": hashes_before,
                    "metadata_hashes_after": hashes_after,
                    "problems": list(problems),
                }
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("dashboard_history_gate_exception")
        errors.append(f"dashboard history switching gate raised {type(error).__name__}: {error}")

    if problems and not errors:
        errors.append(f"same-workspace dashboard history switching smoke failed: {', '.join(problems)}")

    return {
        "name": "dashboard_history_switching_release_gate",
        "schema_version": DASHBOARD_HISTORY_SWITCHING_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 19 Dashboard and Observability Protocol",
            "LoopPlane.md 20 Dashboard UI Specification",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _default_dashboard_history_gate_renderer(
    project: Path,
    *,
    output_dir: Path,
    rebuild_read_models_first: bool,
    workflow_id: str,
) -> Mapping[str, Any]:
    from runtime.dashboard import render_static_dashboard

    return render_static_dashboard(
        project,
        output_dir=output_dir,
        rebuild_read_models_first=rebuild_read_models_first,
        workflow_id=workflow_id,
    )


def _write_dashboard_history_gate_read_models(
    project: Path,
    *,
    workflow_id: str,
    workflow_root: Path,
    status: str,
    title: str,
) -> None:
    read_models_dir = workflow_root / "read_models"
    runtime_dir = workflow_root / "runtime"
    for directory in (
        read_models_dir,
        runtime_dir / "events",
        workflow_root / "requests",
        workflow_root / "results",
        workflow_root / "planning",
        workflow_root / "config",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if workflow_root != project / ".loopplane":
        (workflow_root / "PLAN.md").write_text(
            f"""# Project Plan

## Metadata

- workflow_id: {workflow_id}
- plan_version: 1
- active: false

## Phase P0: Release Gate

- [ ] G001: Historical dashboard fixture
  - acceptance: Historical workflow can be viewed from the same workspace dashboard.
  - evidence: {workflow_root.relative_to(project).as_posix()}/results/G001/
  - latest: {workflow_root.relative_to(project).as_posix()}/results/G001/latest.json
  - depends_on: []
  - risk: low
  - validation: manual
""",
            encoding="utf-8",
        )
    source_hashes = {"events_count": 0, "events_sha256": ""}
    generated_at = "2026-06-12T00:00:00Z"
    workflow_status = {
        "schema_version": "1.6",
        "workflow_id": workflow_id,
        "status": status,
        "phase": "release_gate",
        "summary": title,
        "progress": {
            "total_tasks": 1,
            "completed_tasks": 0 if status == "archived_view" else 1,
            "blocked_tasks": 0,
            "progress_percent": 0.0 if status == "archived_view" else 100.0,
        },
        "generated_at": generated_at,
        "last_event_seq": 0,
        "source_hashes": source_hashes,
    }
    plan_index = {
        "schema_version": "1.6",
        "workflow_id": workflow_id,
        "phases": [
            {
                "phase_id": "P0",
                "title": "Release Gate",
                "status": "done" if status == "active" else "pending",
                "tasks": [
                    {
                        "task_id": "G001",
                        "title": title,
                        "status": "done" if status == "active" else "pending",
                    }
                ],
            }
        ],
        "generated_at": generated_at,
        "last_event_seq": 0,
        "source_hashes": source_hashes,
    }
    workflow_graph = {
        "schema_version": "1.6",
        "workflow_id": workflow_id,
        "nodes": [
            {
                "node_id": "release_gate_dashboard_history",
                "type": "dashboard_view",
                "title": title,
                "status": status,
            }
        ],
        "edges": [],
        "generated_at": generated_at,
        "last_event_seq": 0,
        "source_hashes": source_hashes,
    }
    metrics = {
        "schema_version": "1.6",
        "workflow_id": workflow_id,
        "counts": {"tasks_total": 1, "runs_total": 0, "validations_failed": 0},
        "generated_at": generated_at,
        "last_event_seq": 0,
        "source_hashes": source_hashes,
    }
    version_control = {
        "schema_version": "1.6",
        "workflow_id": workflow_id,
        "status": "unavailable",
        "provider": "git",
        "repository": {"dirty": False, "dirty_files_count": 0},
        "generated_at": generated_at,
        "last_event_seq": 0,
        "source_hashes": source_hashes,
    }
    for filename, payload in {
        "workflow_status.json": workflow_status,
        "plan_index.json": plan_index,
        "workflow_graph.json": workflow_graph,
        "metrics.json": metrics,
        "version_control_status.json": version_control,
    }.items():
        (read_models_dir / filename).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (read_models_dir / "dashboard_feed.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "workflow_id": workflow_id,
                "event": "release_gate_dashboard_history_fixture",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (read_models_dir / "run_summaries.jsonl").write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "workflow_id": workflow_id,
                "run_id": "release_gate_dashboard_history",
                "task_id": "G001",
                "status": status,
                "summary": title,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _dashboard_history_gate_embedded_payload(index_file: Path) -> dict[str, Any]:
    try:
        text = index_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = re.search(r'<script id="loopplane-read-models" type="application/json">(.+?)</script>', text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    if isinstance(data, Mapping) and data.get("payload_encoding") == "gzip+base64":
        compressed = data.get("payload_compressed")
        if not isinstance(compressed, str) or not compressed:
            return {}
        try:
            data = json.loads(gzip.decompress(base64.b64decode(compressed, validate=True)).decode("utf-8"))
        except (binascii.Error, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
    return data if isinstance(data, dict) else {}


def check_archived_read_only_mutation_rejection_release_gate(
    package_root: Path | str | None = None,
    *,
    api_smoke: Any | None = None,
    control_smoke: Any | None = None,
    restore_fork_smoke: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="loopplane-archived-readonly-gate-") as tmp:
            temp_root = Path(tmp)
            project_info = _archived_read_only_gate_prepare_project(temp_root / "dashboard-api")
            protected_roots = {
                project_info["archived_workflow_id"]: project_info["archived_workflow_root"],
                project_info["read_only_workflow_id"]: project_info["read_only_workflow_root"],
            }
            protected_before = _archived_read_only_gate_tree_snapshot(project_info["project"], protected_roots)
            api_fn = api_smoke or _archived_read_only_gate_default_api_smoke
            checked.append(api_fn(project_info, root=root))
            protected_after = _archived_read_only_gate_tree_snapshot(project_info["project"], protected_roots)
            if protected_after != protected_before:
                checked.append(
                    {
                        "name": "protected_workflow_history_mutation_snapshot",
                        "status": "fail",
                        "protected_workflow_ids": sorted(protected_roots),
                        "before_file_count": len(protected_before),
                        "after_file_count": len(protected_after),
                        "added_or_changed": sorted(
                            path
                            for path, digest in protected_after.items()
                            if protected_before.get(path) != digest
                        ),
                        "removed": sorted(path for path in protected_before if path not in protected_after),
                        "problems": ["dashboard_api_mutated_protected_workflow_history"],
                    }
                )
            else:
                checked.append(
                    {
                        "name": "protected_workflow_history_mutation_snapshot",
                        "status": "pass",
                        "protected_workflow_ids": sorted(protected_roots),
                        "before_file_count": len(protected_before),
                        "after_file_count": len(protected_after),
                        "problems": [],
                    }
                )

            control_fn = control_smoke or _archived_read_only_gate_default_control_smoke
            checked.append(control_fn(temp_root / "control", root=root))

            restore_fork_fn = restore_fork_smoke or _archived_read_only_gate_default_restore_fork_smoke
            checked.append(restore_fork_fn(temp_root / "restore-fork", root=root))
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("archived_read_only_mutation_rejection_gate_exception")
        errors.append(f"archived/read-only mutation rejection gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)
        if record.get("status") != "pass" and not record_problems:
            problems.append(f"{record.get('name', 'archived_read_only_gate_case')}_failed")

    if problems and not errors:
        errors.append(f"archived/read-only mutation rejection smoke failed: {', '.join(sorted(set(problems)))}")

    return {
        "name": "archived_read_only_mutation_rejection_release_gate",
        "schema_version": ARCHIVED_READ_ONLY_MUTATION_REJECTION_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 20.2 Top workspace bar",
            "LoopPlane.md 20.5 Bottom inspector chat",
            "LoopPlane.md 20.6 Dashboard API",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31.6 Workflow creation and archival",
            "LoopPlane.md 31.7 Dashboard workflow switching",
            "LoopPlane.md 33.5 Archive migration profile",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
            "LoopPlane.md 33 Installation and Migration Protocol",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _archived_read_only_gate_prepare_project(project: Path) -> dict[str, Any]:
    from runtime.init_workflow import LAYOUT_CANONICAL_V16
    from runtime.path_resolution import WorkflowPaths, load_workflow_config

    init_project(
        project,
        "Release gate fixture for archived/read-only mutation rejection.",
        layout=LAYOUT_CANONICAL_V16,
    )
    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    current_workflow_id = str(paths.workflow_id or workflow.get("workflow_id") or "wf_20260612_17000000")
    archived_workflow_id = _archived_read_only_gate_unique_workflow_id(
        current_workflow_id,
        "wf_20260612_a17a17a1",
    )
    read_only_workflow_id = _archived_read_only_gate_unique_workflow_id(
        current_workflow_id,
        "wf_20260612_b17b17b1",
        {archived_workflow_id},
    )
    workspace = _read_json_if_file(project / ".loopplane" / "workspace.json")
    workspace_id = str(workspace.get("workspace_id") or "ws_archived_read_only_release_gate")

    _write_dashboard_history_gate_read_models(
        project,
        workflow_id=current_workflow_id,
        workflow_root=paths.workflow_root,
        status="active",
        title="mutable release-gate workflow",
    )
    archived_workflow_root = f".loopplane/workflows/{archived_workflow_id}"
    read_only_workflow_root = f".loopplane/workflows/{read_only_workflow_id}"
    _write_dashboard_history_gate_read_models(
        project,
        workflow_id=archived_workflow_id,
        workflow_root=project / archived_workflow_root,
        status="archived",
        title="archived release-gate workflow",
    )
    _write_dashboard_history_gate_read_models(
        project,
        workflow_id=read_only_workflow_id,
        workflow_root=project / read_only_workflow_root,
        status="read_only_imported",
        title="read-only imported release-gate workflow",
    )
    _archived_read_only_gate_enable_approvals(paths)
    _archived_read_only_gate_append_pending_approval(
        paths.runtime_dir,
        workflow_id=current_workflow_id,
        approval_id="approval_mutable_release_gate",
    )
    _archived_read_only_gate_configure_fake_inspector(project, paths)

    registry_path = project / ".loopplane" / "workflow_registry.json"
    registry = _read_json_if_file(registry_path)
    registry["schema_version"] = "1.6"
    registry["workspace_id"] = workspace_id
    registry["generated_at"] = "2026-06-12T00:00:00Z"
    current_record = {
        "workflow_id": current_workflow_id,
        "name": "mutable release-gate workflow",
        "status": "active",
        "workflow_root": paths.workflow_root_value,
        "workflow_config_file": paths.workflow_config_file_value,
        "plan_file": paths.value("plan_file"),
        "read_models_dir": paths.value("read_models_dir"),
        "runtime_dir": paths.value("runtime_dir"),
        "requests_dir": paths.value("requests_dir"),
        "read_only": False,
        "archived": False,
        "policy_eligible": True,
        "summary": {"one_line": "Mutable release-gate workflow.", "tasks_total": 1, "tasks_completed": 1},
    }
    protected_records = [
        {
            "workflow_id": archived_workflow_id,
            "name": "archived release-gate workflow",
            "status": "archived",
            "workflow_root": archived_workflow_root,
            "plan_file": f"{archived_workflow_root}/PLAN.md",
            "read_models_dir": f"{archived_workflow_root}/read_models",
            "runtime_dir": f"{archived_workflow_root}/runtime",
            "requests_dir": f"{archived_workflow_root}/requests",
            "read_only": False,
            "archived": True,
            "policy_eligible": False,
            "summary": {"one_line": "Archived release-gate workflow.", "tasks_total": 1, "tasks_completed": 0},
        },
        {
            "workflow_id": read_only_workflow_id,
            "name": "read-only imported release-gate workflow",
            "status": "read_only_imported",
            "workflow_root": read_only_workflow_root,
            "plan_file": f"{read_only_workflow_root}/PLAN.md",
            "read_models_dir": f"{read_only_workflow_root}/read_models",
            "runtime_dir": f"{read_only_workflow_root}/runtime",
            "requests_dir": f"{read_only_workflow_root}/requests",
            "read_only": True,
            "archived": False,
            "policy_eligible": False,
            "restore_or_fork_required_for_mutation": True,
            "summary": {"one_line": "Read-only release-gate workflow.", "tasks_total": 1, "tasks_completed": 0},
        },
    ]
    registry["workflows"] = [
        current_record,
        *protected_records,
    ]
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    current_path = project / ".loopplane" / "current_workflow.json"
    current_path.write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "workspace_id": workspace_id,
                "current_workflow_id": current_workflow_id,
                "selection_reason": "release_gate_mutable_fixture",
                "updated_at": "2026-06-12T00:00:00Z",
                "updated_by": "release_gate_fixture",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    workspace["schema_version"] = "1.6"
    workspace["workspace_id"] = workspace_id
    workspace["current_workflow_id"] = current_workflow_id
    (project / ".loopplane" / "workspace.json").write_text(
        json.dumps(workspace, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "project": project,
        "paths": paths,
        "workspace_id": workspace_id,
        "current_workflow_id": current_workflow_id,
        "current_workflow_root": paths.workflow_root_value,
        "archived_workflow_id": archived_workflow_id,
        "archived_workflow_root": archived_workflow_root,
        "read_only_workflow_id": read_only_workflow_id,
        "read_only_workflow_root": read_only_workflow_root,
        "approval_id": "approval_mutable_release_gate",
    }


def _archived_read_only_gate_configure_fake_inspector(project: Path, paths: WorkflowPaths) -> None:
    script = project / ".loopplane_agents" / "release_gate_fake_inspector.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        """from __future__ import annotations

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
answer = "Release gate inspector answer: workflow status visible."
if "change request" in prompt.lower():
    answer = "Release gate inspector noted a change request handoff."
response_path = Path(os.environ["LOOPPLANE_INSPECTION_RESPONSE_PATH"])
response_path.parent.mkdir(parents=True, exist_ok=True)
response_path.write_text(
    json.dumps({"answer": answer, "summary": answer, "sources": ["release_gate_fake_inspector"]}) + "\\n",
    encoding="utf-8",
)
print(answer)
""",
        encoding="utf-8",
    )
    runners_path = paths.config_file("agent_runners.json")
    runners = _read_json_if_file(runners_path)
    runner_map = runners.get("runners") if isinstance(runners.get("runners"), Mapping) else {}
    inspector = dict(runner_map.get("inspector")) if isinstance(runner_map.get("inspector"), Mapping) else {}
    inspector.update(
        {
            "adapter": "shell",
            "command": sys.executable,
            "args": [script.as_posix()],
            "cwd": "{{project_root}}",
            "prompt_delivery": {"mode": "stdin"},
            "timeout_seconds": 10,
            "enabled": True,
            "permission_policy": {
                "allow_project_file_edit": True,
                "allow_command_execution": True,
                "require_approval_for_risky_commands": False,
                "read_only": False,
            },
            "doctor": {
                "check_command": f"{sys.executable} --version",
                "check_kind": "doctor_check",
                "requires_auth": False,
            },
        }
    )
    runner_map["inspector"] = inspector
    runners["runners"] = runner_map
    runners_path.write_text(json.dumps(runners, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _archived_read_only_gate_unique_workflow_id(
    current_workflow_id: str,
    candidate: str,
    extra_taken: set[str] | None = None,
) -> str:
    taken = {current_workflow_id, *(extra_taken or set())}
    if candidate not in taken:
        return candidate
    return "wf_20260612_" + uuid.uuid4().hex[:8]


def _archived_read_only_gate_enable_approvals(paths: WorkflowPaths) -> None:
    security_path = paths.config_file("security.json")
    security = _read_json_if_file(security_path)
    approval = security.get("approval") if isinstance(security.get("approval"), Mapping) else {}
    security["approval"] = {**dict(approval), "enabled": True}
    security_path.write_text(json.dumps(security, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _archived_read_only_gate_append_pending_approval(
    runtime_dir: Path,
    *,
    workflow_id: str,
    approval_id: str,
) -> None:
    path = runtime_dir / "human_approval_requests.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = {
        "schema_version": "1.6",
        "approval_id": approval_id,
        "requested_at": "2026-06-12T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "workflow_id": workflow_id,
        "task_id": "G001",
        "type": "task_execution",
        "scope": "release gate approval response",
        "status": "pending",
        "message": "Approve release-gate mutation-record smoke.",
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(request, sort_keys=True) + "\n")


def _archived_read_only_gate_default_api_smoke(
    project_info: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    from runtime.dashboard import create_dashboard_server

    project = Path(project_info["project"])
    current_workflow_id = str(project_info["current_workflow_id"])
    archived_workflow_id = str(project_info["archived_workflow_id"])
    read_only_workflow_id = str(project_info["read_only_workflow_id"])
    approval_id = str(project_info["approval_id"])
    problems: list[str] = []
    protected_results: list[dict[str, Any]] = []
    mutable_results: list[dict[str, Any]] = []

    previous_home = os.environ.get("LOOPPLANE_HOME")
    os.environ["LOOPPLANE_HOME"] = (project / ".loopplane_home_release_gate").as_posix()
    server = None
    thread = None
    try:
        server, startup = create_dashboard_server(
            project,
            port=0,
            host="127.0.0.1",
            workflow_id=current_workflow_id,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        token_file = project / str(startup.get("token_file") or "")
        token = token_file.read_text(encoding="utf-8").strip()
        base_url = f"http://127.0.0.1:{startup['port']}"

        for workflow_id, blocker in (
            (archived_workflow_id, "archived"),
            (read_only_workflow_id, "read_only"),
        ):
            protected_results.append(
                _archived_read_only_gate_api_protected_case(
                    base_url,
                    token=token,
                    workflow_id=workflow_id,
                    expected_blocker=blocker,
                    problems=problems,
                )
            )

        mutable_before = _archived_read_only_gate_record_counts(
            project,
            str(project_info["current_workflow_root"]),
        )
        mutable_results = _archived_read_only_gate_api_mutable_requests(
            base_url,
            token=token,
            workflow_id=current_workflow_id,
            approval_id=approval_id,
            problems=problems,
        )
        mutable_after = _archived_read_only_gate_record_counts(
            project,
            str(project_info["current_workflow_root"]),
        )
        deltas = {
            key: mutable_after.get(key, 0) - mutable_before.get(key, 0)
            for key in sorted(set(mutable_before) | set(mutable_after))
        }
        expected_minimums = {
            "dashboard_requests": 3,
            "control_requests": 4,
            "approval_responses": 1,
            "chat_requests": 1,
            "chat_responses": 1,
            "change_requests": 1,
        }
        shortfalls = {
            key: {"expected_at_least": expected, "actual_delta": deltas.get(key, 0)}
            for key, expected in expected_minimums.items()
            if deltas.get(key, 0) < expected
        }
        if shortfalls:
            problems.append("mutable_dashboard_api_request_records_not_written")
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        if previous_home is None:
            os.environ.pop("LOOPPLANE_HOME", None)
        else:
            os.environ["LOOPPLANE_HOME"] = previous_home

    return {
        "name": "dashboard_api_archived_read_only_mutation_matrix",
        "status": "pass" if not problems else "fail",
        "project_root": _display_path(project, root),
        "current_workflow_id": current_workflow_id,
        "archived_workflow_id": archived_workflow_id,
        "read_only_workflow_id": read_only_workflow_id,
        "protected_results": protected_results,
        "mutable_results": mutable_results,
        "problems": problems,
    }


def _archived_read_only_gate_api_protected_case(
    base_url: str,
    *,
    token: str,
    workflow_id: str,
    expected_blocker: str,
    problems: list[str],
) -> dict[str, Any]:
    dashboard_status, dashboard_payload = _archived_read_only_gate_http_json(
        f"{base_url}/api/workflows/{workflow_id}/dashboard-data",
        token=token,
    )
    controls = {
        "planning": _archived_read_only_gate_mapping(dashboard_payload.get("planning_controls")),
        "execution": _archived_read_only_gate_mapping(dashboard_payload.get("execution_controls")),
        "approval": _archived_read_only_gate_mapping(dashboard_payload.get("approval_controls")),
        "read_model_rebuild": _archived_read_only_gate_mapping(dashboard_payload.get("read_model_rebuild")),
        "inspector_console": _archived_read_only_gate_mapping(dashboard_payload.get("inspector_console")),
    }
    control_blockers: dict[str, list[str]] = {}
    for name, payload in controls.items():
        blockers = [str(item) for item in _archived_read_only_gate_sequence(payload.get("mutation_blockers"))]
        control_blockers[name] = blockers
        if payload.get("mutation_allowed") is not False:
            problems.append(f"{workflow_id}_{name}_control_not_disabled")
        if expected_blocker not in blockers and "read_only_imported" not in blockers:
            problems.append(f"{workflow_id}_{name}_missing_{expected_blocker}_blocker")

    endpoint_results: list[dict[str, Any]] = []
    for label, suffix, body in _archived_read_only_gate_api_mutating_endpoints(workflow_id):
        status_code, response = _archived_read_only_gate_http_json(
            f"{base_url}{suffix}",
            token=token,
            body=body,
        )
        endpoint_results.append(
            {
                "label": label,
                "status_code": status_code,
                "response_status": response.get("status"),
                "ok": response.get("ok"),
            }
        )
        if status_code != 409 or response.get("status") != "read_only_workflow":
            problems.append(f"{workflow_id}_{label}_mutation_not_rejected")

    return {
        "workflow_id": workflow_id,
        "expected_blocker": expected_blocker,
        "dashboard_status_code": dashboard_status,
        "control_blockers": control_blockers,
        "endpoint_results": endpoint_results,
    }


def _archived_read_only_gate_api_mutating_endpoints(workflow_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    base = f"/api/workflows/{workflow_id}"
    endpoints: list[tuple[str, str, dict[str, Any]]] = [
        ("plan", f"{base}/plan", {"runner_id": "planner", "reason": "release gate plan"}),
        ("audit", f"{base}/audit", {"runner_id": "auditor", "reason": "release gate audit"}),
        ("activate_plan", f"{base}/activate-plan", {"plan": "PLAN.md", "reason": "release gate activate"}),
    ]
    for action in ("start", "pause", "resume", "stop"):
        endpoints.append(
            (
                f"control_{action}",
                f"{base}/control-requests",
                {"type": action, "reason": f"release gate {action}"},
            )
        )
    endpoints.extend(
        [
            ("rebuild_read_models", f"{base}/rebuild-read-models", {"reason": "release gate rebuild"}),
            (
                "approval_response",
                f"{base}/approvals/approval_mutable_release_gate/respond",
                {"decision": "approved", "notes": "release gate approval"},
            ),
            ("chat", f"{base}/chat", {"message": "What is the workflow status?"}),
            ("change_request", f"{base}/change-requests", {"user_request": "Add a release-gate follow-up."}),
        ]
    )
    return endpoints


def _archived_read_only_gate_api_mutable_requests(
    base_url: str,
    *,
    token: str,
    workflow_id: str,
    approval_id: str,
    problems: list[str],
) -> list[dict[str, Any]]:
    base = f"/api/workflows/{workflow_id}"
    endpoints: list[tuple[str, str, dict[str, Any]]] = [
        ("plan", f"{base}/plan", {"runner_id": "planner", "reason": "mutable release gate plan"}),
        ("audit", f"{base}/audit", {"runner_id": "auditor", "reason": "mutable release gate audit"}),
        ("activate_plan", f"{base}/activate-plan", {"plan": "PLAN.md", "reason": "mutable release gate activate"}),
    ]
    for action in ("start", "pause", "resume", "stop"):
        endpoints.append(
            (
                f"control_{action}",
                f"{base}/control-requests",
                {"type": action, "reason": f"mutable release gate {action}"},
            )
        )
    endpoints.extend(
        [
            ("rebuild_read_models", f"{base}/rebuild-read-models", {"reason": "mutable release gate rebuild"}),
            (
                "approval_response",
                f"{base}/approvals/{approval_id}/respond",
                {"decision": "approved", "notes": "mutable release gate approval"},
            ),
            ("chat", f"{base}/chat", {"message": "What is the current workflow status?"}),
            (
                "change_request",
                f"{base}/change-requests",
                {"user_request": "Add a mutable release-gate follow-up."},
            ),
        ]
    )
    results: list[dict[str, Any]] = []
    for label, suffix, body in endpoints:
        status_code, response = _archived_read_only_gate_http_json(
            f"{base_url}{suffix}",
            token=token,
            body=body,
        )
        results.append(
            {
                "label": label,
                "status_code": status_code,
                "response_status": response.get("status"),
                "ok": response.get("ok"),
            }
        )
        if status_code != 202 or response.get("ok") is not True:
            problems.append(f"mutable_{label}_request_rejected")
    return results


def _archived_read_only_gate_http_json(
    url: str,
    *,
    token: str,
    body: Mapping[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    headers = {"Authorization": f"Bearer {token}"}
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(dict(body)).encode("utf-8")
        headers["Content-Type"] = "application/json"
        parsed_base = re.match(r"^(https?://[^/]+)", url)
        if parsed_base:
            headers["Origin"] = parsed_base.group(1)
        method = "POST"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return int(response.status), payload if isinstance(payload, dict) else {}
    except HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"ok": False, "status": f"http_{error.code}"}
        return int(error.code), payload if isinstance(payload, dict) else {}


def _archived_read_only_gate_default_control_smoke(temp_root: Path, *, root: Path) -> dict[str, Any]:
    from runtime.control import record_control_request
    from runtime.init_workflow import LAYOUT_CANONICAL_V16
    from runtime.path_resolution import WorkflowPaths, load_workflow_config
    from runtime.workflow_lifecycle import archive_workflow

    problems: list[str] = []
    case_results: list[dict[str, Any]] = []
    actions = ("start", "pause", "resume", "stop")

    mutable_project = temp_root / "mutable"
    init_project(mutable_project, "Mutable control release-gate fixture.", layout=LAYOUT_CANONICAL_V16)
    mutable_workflow = load_workflow_config(mutable_project)
    mutable_paths = WorkflowPaths.from_config(mutable_project, mutable_workflow)
    before = _archived_read_only_gate_record_counts(mutable_project, mutable_paths.workflow_root_value)
    mutable_results = [
        record_control_request(mutable_project, action, source="release_gate_control")
        for action in actions
    ]
    after = _archived_read_only_gate_record_counts(mutable_project, mutable_paths.workflow_root_value)
    if any(result.get("status") != "pending" for result in mutable_results):
        problems.append("mutable_control_requests_rejected")
    if after.get("control_requests", 0) - before.get("control_requests", 0) != len(actions):
        problems.append("mutable_control_requests_not_recorded")
    case_results.append(
        {
            "name": "mutable_control_requests",
            "workflow_id": mutable_paths.workflow_id,
            "statuses": [result.get("status") for result in mutable_results],
            "record_delta": after.get("control_requests", 0) - before.get("control_requests", 0),
        }
    )

    archived_project = temp_root / "archived"
    init_project(archived_project, "Archived control release-gate fixture.", layout=LAYOUT_CANONICAL_V16)
    archived_workflow = load_workflow_config(archived_project)
    archived_paths = WorkflowPaths.from_config(archived_project, archived_workflow)
    archive_workflow(
        archived_project,
        str(archived_paths.workflow_id),
        reason="release gate archived control target",
        updated_by="release_gate",
    )
    before = _archived_read_only_gate_record_counts(archived_project, archived_paths.workflow_root_value)
    archived_results = [
        record_control_request(archived_project, action, source="release_gate_control")
        for action in actions
    ]
    after = _archived_read_only_gate_record_counts(archived_project, archived_paths.workflow_root_value)
    if any(result.get("status") != "archived_workflow" for result in archived_results):
        problems.append("archived_control_requests_not_rejected")
    if after != before:
        problems.append("archived_control_requests_recorded")
    case_results.append(
        {
            "name": "archived_control_requests",
            "workflow_id": archived_paths.workflow_id,
            "statuses": [result.get("status") for result in archived_results],
            "record_counts_before": before,
            "record_counts_after": after,
        }
    )

    read_only_project = temp_root / "read-only"
    init_project(read_only_project, "Read-only control release-gate fixture.", layout=LAYOUT_CANONICAL_V16)
    read_only_workflow = load_workflow_config(read_only_project)
    read_only_paths = WorkflowPaths.from_config(read_only_project, read_only_workflow)
    _archived_read_only_gate_mark_current_read_only(read_only_project, str(read_only_paths.workflow_id))
    before = _archived_read_only_gate_record_counts(read_only_project, read_only_paths.workflow_root_value)
    read_only_results = [
        record_control_request(read_only_project, action, source="release_gate_control")
        for action in actions
    ]
    after = _archived_read_only_gate_record_counts(read_only_project, read_only_paths.workflow_root_value)
    if any(result.get("status") != "read_only_workflow" for result in read_only_results):
        problems.append("read_only_control_requests_not_rejected")
    if after != before:
        problems.append("read_only_control_requests_recorded")
    case_results.append(
        {
            "name": "read_only_control_requests",
            "workflow_id": read_only_paths.workflow_id,
            "statuses": [result.get("status") for result in read_only_results],
            "record_counts_before": before,
            "record_counts_after": after,
        }
    )

    return {
        "name": "workflow_control_archived_read_only_mutation_matrix",
        "status": "pass" if not problems else "fail",
        "project_root": _display_path(temp_root, root),
        "control_actions": list(actions),
        "cases": case_results,
        "problems": problems,
    }


def _archived_read_only_gate_default_restore_fork_smoke(temp_root: Path, *, root: Path) -> dict[str, Any]:
    from runtime.init_workflow import LAYOUT_CANONICAL_V16
    from runtime.path_resolution import load_workflow_config
    from runtime.workflow_lifecycle import archive_workflow, fork_workflow, import_workflow_record, restore_workflow

    problems: list[str] = []
    project = temp_root / "project"
    init_project(project, "Restore/fork release-gate fixture.", layout=LAYOUT_CANONICAL_V16)
    workflow = load_workflow_config(project)
    archived_workflow_id = str(workflow.get("workflow_id") or "")
    archive_workflow(
        project,
        archived_workflow_id,
        reason="release gate restore path",
        updated_by="release_gate",
    )
    restored = restore_workflow(project, archived_workflow_id, updated_by="release_gate")
    restored_record = _archived_read_only_gate_registry_record(project, archived_workflow_id)
    if restored.get("status") != "workflow_restored" or restored_record.get("status") != "active":
        problems.append("archived_workflow_restore_path_failed")
    if restored_record.get("archived") is True or restored_record.get("read_only") is True:
        problems.append("restored_archived_workflow_remained_immutable")

    read_only_workflow_id = _archived_read_only_gate_unique_workflow_id(
        archived_workflow_id,
        "wf_20260612_c17c17c1",
    )
    import_workflow_record(
        project,
        workflow_id=read_only_workflow_id,
        name="Read-only source for release-gate fork",
        workflow_root=f".loopplane/imported/{read_only_workflow_id}",
        updated_by="release_gate",
    )
    source_before = _archived_read_only_gate_registry_record(project, read_only_workflow_id)
    forked_workflow_id = _archived_read_only_gate_unique_workflow_id(
        archived_workflow_id,
        "wf_20260612_d17d17d1",
        {read_only_workflow_id},
    )
    forked = fork_workflow(
        project,
        read_only_workflow_id,
        new_workflow_id=forked_workflow_id,
        name="Mutable fork of read-only import",
        make_current=True,
        updated_by="release_gate",
    )
    source_after = _archived_read_only_gate_registry_record(project, read_only_workflow_id)
    forked_record = _archived_read_only_gate_registry_record(project, forked_workflow_id)
    if forked.get("status") != "workflow_forked" or forked_record.get("status") != "forked":
        problems.append("read_only_workflow_fork_path_failed")
    if forked_record.get("read_only") is True or forked_record.get("archived") is True:
        problems.append("forked_read_only_workflow_remained_immutable")
    if source_after != source_before:
        problems.append("read_only_source_mutated_by_fork")

    return {
        "name": "explicit_restore_or_fork_escape_paths",
        "status": "pass" if not problems else "fail",
        "project_root": _display_path(project, root),
        "restored_workflow_id": archived_workflow_id,
        "restored_record_status": restored_record.get("status"),
        "read_only_source_workflow_id": read_only_workflow_id,
        "forked_workflow_id": forked_workflow_id,
        "forked_record_status": forked_record.get("status"),
        "source_preserved": source_after == source_before,
        "problems": problems,
    }


def _archived_read_only_gate_mark_current_read_only(project: Path, workflow_id: str) -> None:
    registry_path = project / ".loopplane" / "workflow_registry.json"
    registry = _read_json_if_file(registry_path)
    workflows = registry.get("workflows")
    if not isinstance(workflows, list):
        return
    for record in workflows:
        if isinstance(record, dict) and str(record.get("workflow_id") or "") == workflow_id:
            record["status"] = "read_only_imported"
            record["read_only"] = True
            record["archived"] = False
            record["restore_or_fork_required_for_mutation"] = True
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _archived_read_only_gate_registry_record(project: Path, workflow_id: str) -> dict[str, Any]:
    registry = _read_json_if_file(project / ".loopplane" / "workflow_registry.json")
    for record in _archived_read_only_gate_sequence(registry.get("workflows")):
        if isinstance(record, Mapping) and str(record.get("workflow_id") or "") == workflow_id:
            return dict(record)
    return {}


def _archived_read_only_gate_record_counts(project: Path, workflow_root_value: str) -> dict[str, int]:
    root = project / workflow_root_value
    return {
        "dashboard_requests": _archived_read_only_gate_jsonl_count(root / "requests" / "dashboard_requests.jsonl"),
        "control_requests": _archived_read_only_gate_jsonl_count(root / "runtime" / "control_requests.jsonl"),
        "approval_responses": _archived_read_only_gate_jsonl_count(root / "runtime" / "human_approval_responses.jsonl"),
        "chat_requests": _archived_read_only_gate_jsonl_count(root / "requests" / "chat_requests.jsonl"),
        "chat_responses": _archived_read_only_gate_jsonl_count(root / "requests" / "chat_responses.jsonl"),
        "change_requests": _archived_read_only_gate_jsonl_count(root / "requests" / "change_requests.jsonl"),
    }


def _archived_read_only_gate_jsonl_count(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return 0


def _archived_read_only_gate_tree_snapshot(
    project: Path,
    workflow_roots: Mapping[str, str],
) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for workflow_id, workflow_root in workflow_roots.items():
        root = project / workflow_root
        if not root.exists():
            snapshot[f"{workflow_id}:<missing-root>"] = ""
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(project).as_posix()
            snapshot[relative] = _archived_read_only_gate_file_digest(path)
    return snapshot


def _archived_read_only_gate_file_digest(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _archived_read_only_gate_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _archived_read_only_gate_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def check_v16_runtime_schema_version_release_gate(
    package_root: Path | str | None = None,
    *,
    canonical_project_mutator: Any | None = None,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    checked: list[dict[str, Any]] = []
    problems: list[str] = []
    errors: list[str] = []

    try:
        from runtime.init_workflow import LAYOUT_CANONICAL_V16

        with tempfile.TemporaryDirectory(prefix="loopplane-v16-schema-version-gate-") as tmp:
            temp_root = Path(tmp)

            canonical_project = temp_root / "canonical-v16"
            init_project(
                canonical_project,
                "Release gate fixture for canonical v1.6 runtime schema-version checks.",
                layout=LAYOUT_CANONICAL_V16,
            )
            if canonical_project_mutator is not None:
                canonical_project_mutator(canonical_project)
            checked.append(
                _v16_runtime_schema_version_gate_case(
                    canonical_project,
                    root=root,
                    name="canonical_v16_runtime_files",
                )
            )

            flat_project = temp_root / "flat-compatibility"
            init_project(
                flat_project,
                "Release gate fixture for v1.5 flat compatibility schema-version checks.",
            )
            checked.append(
                _v16_runtime_schema_version_gate_case(
                    flat_project,
                    root=root,
                    name="v15_flat_compatibility_files",
                )
            )
    except Exception as error:  # pragma: no cover - serialized in release-gate output.
        problems.append("v16_runtime_schema_version_gate_exception")
        errors.append(f"v1.6 runtime schema-version gate raised {type(error).__name__}: {error}")

    for record in checked:
        record_problems = record.get("problems")
        if isinstance(record_problems, Sequence) and not isinstance(record_problems, (str, bytes)):
            problems.extend(str(problem) for problem in record_problems)

    if problems and not errors:
        errors.append(f"stale v1.5 schema-version runtime emission(s): {', '.join(sorted(set(problems)))}")

    return {
        "name": "v16_runtime_schema_version_release_gate",
        "schema_version": V16_RUNTIME_SCHEMA_VERSION_GATE_SCHEMA_VERSION,
        "status": "pass" if not problems and not errors else "fail",
        "spec_sources": [
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31 Same-Workspace Workflow History Protocol",
            "LoopPlane.md 9.42 schema_version.json",
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 9.42 schema_version.json",
        ],
        "checked": checked,
        "problems": sorted(set(problems)),
        "errors": errors,
    }


def _v16_runtime_schema_version_gate_case(
    project: Path,
    *,
    root: Path,
    name: str,
) -> dict[str, Any]:
    from runtime.path_resolution import WorkflowPaths, load_workflow_config

    workflow = load_workflow_config(project)
    paths = WorkflowPaths.from_config(project, workflow)
    compatibility_tags = _v16_schema_compatibility_tags(project)
    findings = _schema_version_15_findings(project)
    untagged: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []

    for finding in findings:
        path = str(finding.get("path") or "")
        if paths.workflow_root_value.rstrip("/") == ".loopplane" and _path_is_in_flat_workflow(path):
            allowed.append({**finding, "allowance": "v1.5_flat_layout_compatibility"})
            continue
        if _finding_has_schema_compatibility_tag(finding, compatibility_tags):
            allowed.append({**finding, "allowance": "explicit_schema_version_compatibility_tag"})
            continue
        untagged.append(finding)

    problems = [
        f"untagged_stale_schema_version:{finding.get('path')}"
        + (f":{finding.get('line')}" if finding.get("line") is not None else "")
        for finding in untagged
    ]
    return {
        "name": name,
        "status": "pass" if not untagged else "fail",
        "project_root": _display_path(project, root),
        "workflow_id": paths.workflow_id,
        "workflow_root": paths.workflow_root_value,
        "schema_version_15_findings": findings,
        "allowed_schema_version_15_findings": allowed,
        "untagged_schema_version_15_findings": untagged,
        "compatibility_tag_count": len(compatibility_tags),
        "problems": problems,
    }


def _schema_version_15_findings(project: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    root = project / ".loopplane"
    if not root.exists():
        return findings
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project).as_posix()
        if path.suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping) and payload.get("schema_version") == "1.5":
                findings.append({"path": relative, "line": None, "kind": "json"})
            continue
        if path.suffix == ".jsonl":
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, Mapping) and payload.get("schema_version") == "1.5":
                    findings.append({"path": relative, "line": index, "kind": "jsonl"})
    return findings


def _v16_schema_compatibility_tags(project: Path) -> set[str]:
    tagged: set[str] = set()
    for path in sorted((project / ".loopplane").rglob("schema_version.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping) or payload.get("schema_version") != "1.6":
            continue
        compatibility = payload.get("compatibility")
        if not isinstance(compatibility, Mapping):
            continue
        if compatibility.get("legacy_schema_version") != "1.5":
            continue
        if compatibility.get("status") not in {"compatibility_tagged", "legacy_schema_compatibility"}:
            continue
        files = compatibility.get("legacy_schema_version_files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            continue
        for value in files:
            if isinstance(value, str) and value:
                tagged.add(value)
    return tagged


def _finding_has_schema_compatibility_tag(finding: Mapping[str, Any], tagged_paths: set[str]) -> bool:
    path = str(finding.get("path") or "")
    return path in tagged_paths


def _path_is_in_flat_workflow(path: str) -> bool:
    return path.startswith(".loopplane/")


def _file_sha256(path: Path) -> str:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def check_docs_completed_requirements_not_stubbed(
    package_root: Path | str | None = None,
    *,
    doc_files: Sequence[str] = PRIMARY_USER_DOC_FILES,
    completed_surfaces: Sequence[Mapping[str, Any]] = COMPLETED_REQUIREMENT_DOC_SURFACES,
    acceptable_contexts: Sequence[Mapping[str, Any]] = ACCEPTABLE_FUTURE_DOC_CONTEXTS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    findings: list[dict[str, Any]] = []
    stale_claims: list[dict[str, Any]] = []
    accepted_mentions: list[dict[str, Any]] = []
    missing_docs: list[str] = []

    for relative in doc_files:
        path = root / relative
        if not path.is_file():
            missing_docs.append(relative)
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            matched_terms = _matching_stale_doc_terms(line)
            if not matched_terms:
                continue
            context_text = _line_context_text(lines, index, window=1)
            surface_matches = _matching_doc_contexts(context_text, completed_surfaces)
            acceptable_matches = _matching_doc_contexts(context_text, acceptable_contexts)
            status = "accepted_future_or_deferred_reference"
            problem = None
            if surface_matches:
                status = "fail"
                problem = "stale_completed_requirement_claim"
            elif not acceptable_matches:
                status = "fail"
                problem = "unclassified_stale_completion_language"
            record = {
                "file": relative,
                "line": index + 1,
                "status": status,
                "problem": problem,
                "matched_terms": matched_terms,
                "matched_completed_surfaces": surface_matches,
                "matched_acceptable_contexts": acceptable_matches,
                "text": line.strip(),
                "context": context_text,
            }
            findings.append(record)
            if status == "fail":
                stale_claims.append(record)
            else:
                accepted_mentions.append(record)

    errors = [
        (
            f"{entry['file']}:{entry['line']}: {entry['problem']} "
            f"({', '.join(entry['matched_terms'])})"
        )
        for entry in stale_claims
    ]
    if missing_docs:
        errors.append(f"missing user-facing documentation file(s): {', '.join(missing_docs)}")

    return {
        "name": "docs_completed_requirements_not_stubbed",
        "schema_version": DOC_CONSISTENCY_SCHEMA_VERSION,
        "status": "pass" if not stale_claims and not missing_docs else "fail",
        "spec_sources": [
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 28 Testing Plan",
            "LoopPlane.md 26 MVP Scope",
        ],
        "docs_checked": list(doc_files),
        "missing_docs": missing_docs,
        "stale_phrase_patterns": [
            {"id": str(entry["id"]), "pattern": str(entry["pattern"])}
            for entry in STALE_COMPLETION_DOC_PATTERNS
        ],
        "completed_surfaces": [
            {
                "id": str(entry.get("id") or ""),
                "label": str(entry.get("label") or ""),
                "completed_scope": str(entry.get("completed_scope") or ""),
                "aliases": [str(alias) for alias in entry.get("aliases", ())],
            }
            for entry in completed_surfaces
        ],
        "acceptable_future_or_deferred_contexts": [
            {
                "id": str(entry.get("id") or ""),
                "label": str(entry.get("label") or ""),
                "aliases": [str(alias) for alias in entry.get("aliases", ())],
            }
            for entry in acceptable_contexts
        ],
        "findings": findings,
        "stale_completed_requirement_claims": stale_claims,
        "accepted_future_or_deferred_mentions": accepted_mentions,
        "counts": {
            "docs_checked": len(doc_files),
            "missing_docs": len(missing_docs),
            "stale_language_mentions": len(findings),
            "stale_completed_requirement_claims": len(stale_claims),
            "accepted_future_or_deferred_mentions": len(accepted_mentions),
        },
        "errors": errors,
    }


def check_docs_smoke_examples_are_not_substitutes(
    package_root: Path | str | None = None,
    *,
    doc_files: Sequence[str] = SMOKE_EXAMPLE_DOC_FILES,
    risk_patterns: Sequence[Mapping[str, Any]] = SMOKE_EXAMPLE_RISK_PATTERNS,
    required_clarifications: Sequence[Mapping[str, Any]] = SMOKE_EXAMPLE_REQUIRED_CLARIFICATIONS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    missing_docs: list[str] = []
    risky_claims: list[dict[str, Any]] = []

    doc_texts: dict[str, str] = {}
    for relative in doc_files:
        path = root / relative
        if not path.is_file():
            missing_docs.append(relative)
            doc_texts[relative] = ""
            continue
        text = path.read_text(encoding="utf-8")
        doc_texts[relative] = text
        lines = text.splitlines()
        for index, line in enumerate(lines):
            matched_patterns = _matching_smoke_risk_patterns(line, risk_patterns)
            if not matched_patterns:
                continue
            risky_claims.append(
                {
                    "file": relative,
                    "line": index + 1,
                    "matched_patterns": matched_patterns,
                    "text": line.strip(),
                    "context": _line_context_text(lines, index, window=1),
                    "problem": "smoke_example_substitute_claim",
                }
            )

    clarification_records: list[dict[str, Any]] = []
    missing_clarifications: list[dict[str, Any]] = []
    for entry in required_clarifications:
        relative = str(entry.get("file") or "")
        terms = tuple(str(term) for term in entry.get("required_terms", ()) if str(term))
        text = doc_texts.get(relative)
        if text is None:
            path = root / relative
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            doc_texts[relative] = text
        missing_terms = [
            term
            for term in terms
            if _normalize_doc_text(term) not in _normalize_doc_text(text)
        ]
        record = {
            "id": str(entry.get("id") or ""),
            "file": relative,
            "required_terms": list(terms),
            "missing_terms": missing_terms,
            "status": "pass" if not missing_terms else "fail",
        }
        clarification_records.append(record)
        if missing_terms:
            missing_clarifications.append(record)

    errors = [
        (
            f"{entry['file']}:{entry['line']}: {entry['problem']} "
            f"({', '.join(entry['matched_patterns'])})"
        )
        for entry in risky_claims
    ]
    if missing_docs:
        errors.append(f"missing user-facing documentation file(s): {', '.join(missing_docs)}")
    for entry in missing_clarifications:
        errors.append(
            f"{entry['file']}: missing smoke-example clarification {entry['id']} "
            f"term(s): {', '.join(entry['missing_terms'])}"
        )

    return {
        "name": "docs_smoke_examples_not_substitutes",
        "schema_version": DOC_SMOKE_EXAMPLES_SCHEMA_VERSION,
        "status": "pass" if not risky_claims and not missing_docs and not missing_clarifications else "fail",
        "spec_sources": [
            "LoopPlane.md 2.2 CLI flow",
            "LoopPlane.md 2.3 Dashboard flow",
            "LoopPlane.md 11 Agent Runner Adapter Contract",
            "LoopPlane.md 19-20 Dashboard protocol and UI",
            "LoopPlane.md 25 command surfaces",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 28 Testing Plan",
        ],
        "docs_checked": list(doc_files),
        "missing_docs": missing_docs,
        "risk_patterns": [
            {"id": str(entry.get("id") or ""), "pattern": str(entry.get("pattern") or "")}
            for entry in risk_patterns
        ],
        "required_clarifications": clarification_records,
        "missing_required_clarifications": missing_clarifications,
        "risky_substitute_claims": risky_claims,
        "counts": {
            "docs_checked": len(doc_files),
            "missing_docs": len(missing_docs),
            "risky_substitute_claims": len(risky_claims),
            "required_clarifications": len(clarification_records),
            "missing_required_clarifications": len(missing_clarifications),
        },
        "errors": errors,
    }


def check_docs_status_classification_language(
    package_root: Path | str | None = None,
    *,
    doc_files: Sequence[str] = STATUS_CLASSIFICATION_DOC_FILES,
    required_clarifications: Sequence[Mapping[str, Any]] = STATUS_CLASSIFICATION_REQUIRED_CLARIFICATIONS,
    overclaim_patterns: Sequence[Mapping[str, Any]] = STATUS_CLASSIFICATION_OVERCLAIM_PATTERNS,
    boundary_terms: Sequence[str] = STATUS_CLASSIFICATION_BOUNDARY_TERMS,
) -> dict[str, Any]:
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    missing_docs: list[str] = []
    future_overclaims: list[dict[str, Any]] = []

    doc_texts: dict[str, str] = {}
    for relative in doc_files:
        path = root / relative
        if not path.is_file():
            missing_docs.append(relative)
            doc_texts[relative] = ""
            continue
        text = path.read_text(encoding="utf-8")
        doc_texts[relative] = text
        lines = text.splitlines()
        for index, line in enumerate(lines):
            matched_patterns = _matching_smoke_risk_patterns(line, overclaim_patterns)
            if not matched_patterns:
                continue
            context = _line_context_text(lines, index, window=1)
            matched_boundaries = _matching_boundary_terms(context, boundary_terms)
            if matched_boundaries:
                continue
            future_overclaims.append(
                {
                    "file": relative,
                    "line": index + 1,
                    "matched_patterns": matched_patterns,
                    "text": line.strip(),
                    "context": context,
                    "problem": "future_surface_overclaim",
                }
            )

    clarification_records: list[dict[str, Any]] = []
    missing_clarifications: list[dict[str, Any]] = []
    for entry in required_clarifications:
        relative = str(entry.get("file") or "")
        terms = tuple(str(term) for term in entry.get("required_terms", ()) if str(term))
        text = doc_texts.get(relative)
        if text is None:
            path = root / relative
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            doc_texts[relative] = text
        missing_terms = [
            term
            for term in terms
            if _normalize_doc_text(term) not in _normalize_doc_text(text)
        ]
        record = {
            "id": str(entry.get("id") or ""),
            "file": relative,
            "required_terms": list(terms),
            "missing_terms": missing_terms,
            "status": "pass" if not missing_terms else "fail",
        }
        clarification_records.append(record)
        if missing_terms:
            missing_clarifications.append(record)

    errors = [
        (
            f"{entry['file']}:{entry['line']}: {entry['problem']} "
            f"({', '.join(entry['matched_patterns'])})"
        )
        for entry in future_overclaims
    ]
    if missing_docs:
        errors.append(f"missing user-facing documentation file(s): {', '.join(missing_docs)}")
    for entry in missing_clarifications:
        errors.append(
            f"{entry['file']}: missing implementation-status clarification {entry['id']} "
            f"term(s): {', '.join(entry['missing_terms'])}"
        )

    return {
        "name": "docs_status_classification_language",
        "schema_version": DOC_STATUS_CLASSIFICATION_SCHEMA_VERSION,
        "status": "pass"
        if not future_overclaims and not missing_docs and not missing_clarifications
        else "fail",
        "spec_sources": [
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 26 MVP Scope",
            "LoopPlane.md 31-33 v1.6 workspace, multi-project, and migration protocols",
            "LoopPlane.md 0 v1.6 Revision Notes",
            "LoopPlane.md 31-33 v1.6 workspace, multi-project, and migration protocols",
        ],
        "docs_checked": list(doc_files),
        "missing_docs": missing_docs,
        "overclaim_patterns": [
            {"id": str(entry.get("id") or ""), "pattern": str(entry.get("pattern") or "")}
            for entry in overclaim_patterns
        ],
        "boundary_terms": [str(term) for term in boundary_terms],
        "required_clarifications": clarification_records,
        "missing_required_clarifications": missing_clarifications,
        "future_overclaim_claims": future_overclaims,
        "counts": {
            "docs_checked": len(doc_files),
            "missing_docs": len(missing_docs),
            "future_overclaim_claims": len(future_overclaims),
            "required_clarifications": len(clarification_records),
            "missing_required_clarifications": len(missing_clarifications),
        },
        "errors": errors,
    }


def install_skill_project(
    target: Path | str,
    *,
    agent_styles: Sequence[str] | None = None,
    project_agent_skills: bool = True,
) -> dict[str, Any]:
    started_at = _utc_now()
    project = Path(target).expanduser().resolve()
    selected_agent_targets = _selected_agent_skill_install_targets(
        agent_styles,
        project_agent_skills=project_agent_skills,
    )
    if project.exists() and not project.is_dir():
        return _install_failure(
            project=project,
            started_at=started_at,
            status="refused",
            errors=[f"{project}: exists and is not a directory"],
            conflicts=[f"{project}: exists and is not a directory"],
        )

    created: list[str] = []
    preserved: list[str] = []
    created_directories: list[str] = []
    agent_skill_installations: list[dict[str, Any]] = []
    init_result: InitResult | None = None
    try:
        project.mkdir(parents=True, exist_ok=True)
        _preflight_agent_skill_projection_conflicts(project, mode="install", targets=selected_agent_targets)
        if (project / ".loopplane" / "config" / "workflow.json").is_file():
            workflow = _load_workflow(project)
        else:
            _preflight_new_install_metadata(project)
            init_result = init_project(project, INSTALL_BRIEF)
            created.extend(init_result.created)
            preserved.extend(init_result.preserved)
            workflow = _load_workflow(project)

        paths = WorkflowPaths.from_config(project, workflow)
        created_directories.extend(_ensure_install_directories(project, paths))
        workspace = _ensure_workspace_identity(project, workflow, started_at, created, preserved)
        _ensure_install_metadata_files(
            project=project,
            workflow=workflow,
            paths=paths,
            workspace=workspace,
            installed_at=started_at,
            created=created,
            preserved=preserved,
        )
        agent_skill_installations = _ensure_agent_skill_projections(
            project,
            mode="install",
            created=created,
            updated=None,
            preserved=preserved,
            created_directories=created_directories,
            targets=selected_agent_targets,
        )
    except InitConflictError as error:
        return _install_failure(
            project=project,
            started_at=started_at,
            status="refused",
            errors=["Install refused because one or more project files would be overwritten."],
            conflicts=error.conflicts,
        )
    except SkillInstallConflictError as error:
        return _install_failure(
            project=project,
            started_at=started_at,
            status="refused",
            errors=["Install refused because one or more project-local metadata files conflict."],
            conflicts=error.conflicts,
        )
    except (OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return _install_failure(
            project=project,
            started_at=started_at,
            status="invalid_config",
            errors=[str(error)],
            conflicts=[],
        )

    runner_readiness = _install_runner_readiness(project, auto_configure=True)
    schema_validation = validate_project_schemas(project)
    version_control_status = init_result.version_control_status if init_result else _runtime_version_control_status(paths)
    version_control_problem = init_result.version_control_problem if init_result else _runtime_version_control_problem(paths)
    ok = (
        bool(schema_validation.get("ok"))
        and version_control_status != "waiting_config"
        and bool(runner_readiness.get("ok"))
    )
    status = "installed" if init_result else "attached"
    if not schema_validation.get("ok"):
        status = str(schema_validation.get("status") or "invalid_config")
    elif version_control_status == "waiting_config":
        status = "installed_waiting_config" if init_result else "attached_waiting_config"
    elif not runner_readiness.get("ok"):
        status = "installed_waiting_config" if init_result else "attached_waiting_config"

    warnings = [
        "Installed a v1.6-compatible flat workflow-root instance; canonical "
        ".loopplane/workflows/<workflow_id>/ roots are available through explicit "
        "workflow create, fork, or migration/import commands."
    ]
    if not runner_readiness.get("ok"):
        warnings.append(
            "Install-time CLI runner readiness is waiting_config; a setup agent must "
            "find, configure, and doctor the required Codex or Claude Code CLI before planning."
        )
    errors = list(schema_validation.get("errors", [])) if not schema_validation.get("ok") else []
    if not runner_readiness.get("ok"):
        errors.extend(str(error) for error in runner_readiness.get("errors", []))

    return {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": str(workflow.get("workflow_id") or ""),
        "workspace_id": workspace.workspace_id,
        "layout": INSTALL_INSTANCE_PROFILE,
        "workflow_root": ".loopplane",
        "created": sorted(set(created)),
        "preserved": sorted(set(preserved)),
        "created_directories": sorted(set(created_directories)),
        "schema_validation": schema_validation,
        "version_control_status": version_control_status,
        "version_control_problem": version_control_problem,
        "runner_readiness": runner_readiness,
        "agent_skill_projection_policy": {
            "enabled": bool(project_agent_skills),
            "requested_agent_styles": list(agent_styles or []),
            "installed_agent_styles": [str(target["agent_style"]) for target in selected_agent_targets],
        },
        "agent_skill_installations": agent_skill_installations,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": warnings,
        "errors": _dedupe_strings(errors),
        "conflicts": [],
    }


def update_skill_project(target: Path | str) -> dict[str, Any]:
    started_at = _utc_now()
    project = Path(target).expanduser().resolve()
    if not project.is_dir():
        return _update_failure(
            project=project,
            started_at=started_at,
            status="invalid_config",
            errors=[f"{project}: target project does not exist or is not a directory"],
            conflicts=[],
        )
    has_flat_workflow = (project / ".loopplane" / "config" / "workflow.json").is_file()
    has_current_workflow_metadata = (
        (project / ".loopplane" / "current_workflow.json").is_file()
        and (project / ".loopplane" / "workflow_registry.json").is_file()
    )
    if not has_flat_workflow and not has_current_workflow_metadata:
        return _update_failure(
            project=project,
            started_at=started_at,
            status="invalid_config",
            errors=[f"{project / '.loopplane' / 'config' / 'workflow.json'}: workflow config is missing"],
            conflicts=[],
        )

    created: list[str] = []
    updated: list[str] = []
    preserved: list[str] = []
    created_directories: list[str] = []
    agent_skill_installations: list[dict[str, Any]] = []
    layout = INSTALL_INSTANCE_PROFILE
    workflow_root_value = ".loopplane"
    warnings = [
        "Updated LoopPlane-managed metadata for the current workflow root.",
        "Project brief, plan, shared context, runtime state, requests, approvals, "
        "read models, results, checkpoints, logs, and config/local files are preserved.",
    ]
    try:
        workflow = load_workflow_config(project)
        paths = WorkflowPaths.from_config(project, workflow)
        workflow_root_value = paths.workflow_root_value.rstrip("/")
        if workflow_root_value != ".loopplane":
            layout = "canonical_v16"
        _preflight_agent_skill_projection_conflicts(project, mode="update")
        created_directories.extend(_ensure_install_directories(project, paths))
        workspace = _ensure_workspace_identity(project, workflow, started_at, created, preserved)
        _ensure_update_metadata_files(
            project=project,
            workflow=workflow,
            paths=paths,
            workspace=workspace,
            updated_at=started_at,
            created=created,
            updated=updated,
            preserved=preserved,
        )
        agent_skill_installations = _ensure_agent_skill_projections(
            project,
            mode="update",
            created=created,
            updated=updated,
            preserved=preserved,
            created_directories=created_directories,
        )
    except (OSError, json.JSONDecodeError, WorkflowPathError, SkillInstallConflictError) as error:
        conflicts = getattr(error, "conflicts", ())
        return _update_failure(
            project=project,
            started_at=started_at,
            status="invalid_config",
            errors=[str(error)],
            conflicts=conflicts if isinstance(conflicts, Sequence) else [],
        )
    except SkillUpdateConflictError as error:
        return _update_failure(
            project=project,
            started_at=started_at,
            status="refused",
            errors=["Update refused because a package-managed metadata file has an unknown local version."],
            conflicts=error.conflicts,
        )

    runner_readiness = _install_runner_readiness(project, auto_configure=True)
    schema_validation = validate_project_schemas(project)
    version_control_status = _runtime_version_control_status(paths)
    version_control_problem = _runtime_version_control_problem(paths)
    ok = (
        bool(schema_validation.get("ok"))
        and version_control_status != "waiting_config"
        and bool(runner_readiness.get("ok"))
    )
    touched = bool(created or updated or created_directories)
    status = "updated" if touched else "current"
    if not schema_validation.get("ok"):
        status = str(schema_validation.get("status") or "invalid_config")
    elif version_control_status == "waiting_config":
        status = "updated_waiting_config" if touched else "current_waiting_config"
    elif not runner_readiness.get("ok"):
        status = "updated_waiting_config" if touched else "current_waiting_config"

    if not runner_readiness.get("ok"):
        warnings.append(
            "Update-time CLI runner readiness is waiting_config; a setup agent must "
            "find, configure, and doctor the required Codex or Claude Code CLI before planning."
        )
    errors = list(schema_validation.get("errors", [])) if not schema_validation.get("ok") else []
    if not runner_readiness.get("ok"):
        errors.extend(str(error) for error in runner_readiness.get("errors", []))

    return {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "ok": ok,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": str(workflow.get("workflow_id") or ""),
        "workspace_id": workspace.workspace_id,
        "layout": layout,
        "workflow_root": workflow_root_value,
        "created": sorted(set(created)),
        "updated": sorted(set(updated)),
        "preserved": sorted(set(preserved)),
        "created_directories": sorted(set(created_directories)),
        "protected_paths": _protected_project_paths(paths),
        "schema_validation": schema_validation,
        "version_control_status": version_control_status,
        "version_control_problem": version_control_problem,
        "runner_readiness": runner_readiness,
        "agent_skill_installations": agent_skill_installations,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": warnings,
        "errors": _dedupe_strings(errors),
        "conflicts": [],
    }


def pack_skill_package(
    package_root: Path | str | None = None,
    *,
    output: Path | str | None = None,
) -> dict[str, Any]:
    started_at = _utc_now()
    root = Path(package_root or PACKAGE_ROOT).expanduser().resolve()
    validation = doctor_skill_package(root)
    metadata = _metadata_from_doctor_result(validation)
    metadata_summary = _metadata_summary(metadata)
    archive_root = _archive_root_name(metadata_summary)
    artifact_path = _resolve_pack_output(root, output, metadata_summary)

    if not validation.get("ok"):
        return _pack_failure(
            root=root,
            artifact_path=artifact_path,
            started_at=started_at,
            status="invalid_package",
            validation=validation,
            metadata_summary=metadata_summary,
            errors=list(validation.get("errors") or ["Package validation failed."]),
        )

    try:
        included_files = _collect_package_files(root, metadata)
        if not included_files:
            return _pack_failure(
                root=root,
                artifact_path=artifact_path,
                started_at=started_at,
                status="invalid_package",
                validation=validation,
                metadata_summary=metadata_summary,
                errors=["No portable package files were selected for the artifact."],
            )
        counts = _package_content_counts(root, included_files)
        manifest = {
            "schema_version": PACK_MANIFEST_SCHEMA_VERSION,
            "archive_format": "zip",
            "archive_root": archive_root,
            "metadata": metadata_summary,
            "content_counts": counts,
            "validation_status": validation.get("status"),
            "validation_schema_version": validation.get("schema_version"),
            "required_files_checked": list(validation.get("required_files_checked") or []),
            "required_dirs_checked": list(validation.get("required_dirs_checked") or []),
        }
        _write_package_zip(root, artifact_path, archive_root, included_files)
        artifact_sha256 = _file_sha256(artifact_path)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        return _pack_failure(
            root=root,
            artifact_path=artifact_path,
            started_at=started_at,
            status="failed",
            validation=validation,
            metadata_summary=metadata_summary,
            errors=[str(error)],
        )

    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "ok": True,
        "status": "packed",
        "package_root": root.as_posix(),
        "artifact_path": artifact_path.as_posix(),
        "artifact_sha256": f"sha256:{artifact_sha256}",
        "archive_format": "zip",
        "archive_root": archive_root,
        "metadata": metadata_summary,
        "manifest": manifest,
        "content_counts": counts,
        "validation_status": validation.get("status"),
        "validation": validation,
        "included_files": included_files,
        "excluded_roots": sorted(PACKAGE_EXCLUDED_ROOTS),
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": list(validation.get("warnings") or []),
        "errors": [],
    }


def skill_doctor_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") else EXIT_INVALID_CONFIG


def skill_install_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") else EXIT_INVALID_CONFIG


def skill_update_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") else EXIT_INVALID_CONFIG


def skill_pack_exit_code(result: Mapping[str, Any]) -> int:
    return EXIT_SUCCESS if result.get("ok") else EXIT_INVALID_CONFIG


def format_skill_doctor_text(result: Mapping[str, Any]) -> str:
    status = str(result.get("status") or "unknown")
    lines = [
        f"LoopPlane skill package doctor: {status}",
        f"Package root: {result.get('package_root')}",
        (
            "Checked "
            f"{len(result.get('required_files_checked') or [])} required files and "
            f"{len(result.get('required_dirs_checked') or [])} required directories."
        ),
    ]

    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping):
                continue
            lines.append(f"- {check.get('name')}: {check.get('status')}")

    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  - {error}")
    else:
        lines.append("All required package diagnostics passed.")

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines) + "\n"


def format_skill_install_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane skill install: {result.get('status', 'unknown')}",
        f"target: {result.get('project_root')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"layout: {result.get('layout') or 'unknown'}",
        f"workflow_root: {result.get('workflow_root') or 'unknown'}",
    ]

    created = result.get("created")
    if isinstance(created, Sequence) and not isinstance(created, (str, bytes)):
        lines.append(f"created_files: {len(created)}")
    directories = result.get("created_directories")
    if isinstance(directories, Sequence) and not isinstance(directories, (str, bytes)):
        lines.append(f"created_directories: {len(directories)}")
    preserved = result.get("preserved")
    if isinstance(preserved, Sequence) and not isinstance(preserved, (str, bytes)) and preserved:
        lines.append(f"preserved_files: {len(preserved)}")

    version_control_status = result.get("version_control_status")
    if version_control_status:
        lines.append(f"version_control_status: {version_control_status}")
    schema_validation = result.get("schema_validation")
    if isinstance(schema_validation, Mapping):
        lines.append(f"schema_validation: {schema_validation.get('status')}")
    _append_runner_readiness_text(lines, result.get("runner_readiness"))
    _append_agent_skill_installation_text(lines, result.get("agent_skill_installations"))

    conflicts = result.get("conflicts")
    if isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and conflicts:
        lines.append("Conflicts:")
        for conflict in conflicts:
            lines.append(f"  - {conflict}")
        lines.append("Use an explicit migration or approval path before changing these files.")

    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  - {error}")

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines) + "\n"


def format_skill_update_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"loopplane skill update: {result.get('status', 'unknown')}",
        f"target: {result.get('project_root')}",
        f"workflow_id: {result.get('workflow_id') or 'unknown'}",
        f"workspace_id: {result.get('workspace_id') or 'unknown'}",
        f"layout: {result.get('layout') or 'unknown'}",
        f"workflow_root: {result.get('workflow_root') or 'unknown'}",
    ]

    created = result.get("created")
    if isinstance(created, Sequence) and not isinstance(created, (str, bytes)):
        lines.append(f"created_files: {len(created)}")
    updated = result.get("updated")
    if isinstance(updated, Sequence) and not isinstance(updated, (str, bytes)):
        lines.append(f"updated_files: {len(updated)}")
    directories = result.get("created_directories")
    if isinstance(directories, Sequence) and not isinstance(directories, (str, bytes)):
        lines.append(f"created_directories: {len(directories)}")
    preserved = result.get("preserved")
    if isinstance(preserved, Sequence) and not isinstance(preserved, (str, bytes)) and preserved:
        lines.append(f"preserved_files: {len(preserved)}")

    protected = result.get("protected_paths")
    if isinstance(protected, Sequence) and not isinstance(protected, (str, bytes)):
        lines.append(f"protected_paths: {len(protected)}")
    version_control_status = result.get("version_control_status")
    if version_control_status:
        lines.append(f"version_control_status: {version_control_status}")
    schema_validation = result.get("schema_validation")
    if isinstance(schema_validation, Mapping):
        lines.append(f"schema_validation: {schema_validation.get('status')}")
    _append_runner_readiness_text(lines, result.get("runner_readiness"))
    _append_agent_skill_installation_text(lines, result.get("agent_skill_installations"))

    conflicts = result.get("conflicts")
    if isinstance(conflicts, Sequence) and not isinstance(conflicts, (str, bytes)) and conflicts:
        lines.append("Conflicts:")
        for conflict in conflicts:
            lines.append(f"  - {conflict}")
        lines.append("Use an explicit migration or approval path before changing these files.")

    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  - {error}")

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines) + "\n"


def _append_runner_readiness_text(lines: list[str], readiness: Any) -> None:
    if not isinstance(readiness, Mapping):
        return
    lines.append(f"runner_readiness: {readiness.get('status')}")
    local_override = readiness.get("local_override_path")
    if local_override:
        lines.append(f"runner_local_override: {local_override}")
    configured = readiness.get("configured_runner_ids")
    if isinstance(configured, Sequence) and not isinstance(configured, (str, bytes)) and configured:
        lines.append("runner_configured: " + ", ".join(str(item) for item in configured))
    next_steps = readiness.get("next_steps")
    if isinstance(next_steps, Sequence) and not isinstance(next_steps, (str, bytes)) and next_steps:
        lines.append("Runner next steps:")
        for step in next_steps:
            lines.append(f"  - {step}")


def _append_agent_skill_installation_text(lines: list[str], installations: Any) -> None:
    if not isinstance(installations, Sequence) or isinstance(installations, (str, bytes)):
        return
    if not installations:
        return
    lines.append("agent_skill_installations:")
    for entry in installations:
        if not isinstance(entry, Mapping):
            continue
        lines.append(
            "  - "
            f"{entry.get('agent_style')}: {entry.get('status')} "
            f"({entry.get('skill_root')})"
        )


def format_skill_pack_text(result: Mapping[str, Any]) -> str:
    metadata = result.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    counts = result.get("content_counts")
    if not isinstance(counts, Mapping):
        counts = {}
    lines = [
        f"loopplane skill pack: {result.get('status', 'unknown')}",
        f"package: {metadata.get('name') or 'unknown'} {metadata.get('version') or 'unknown'}",
        f"package_root: {result.get('package_root')}",
        f"artifact: {result.get('artifact_path')}",
        f"validation: {result.get('validation_status') or 'unknown'}",
        f"files: {counts.get('files', 0)}",
        f"directories: {counts.get('directories', 0)}",
        f"bytes: {counts.get('bytes', 0)}",
    ]
    if result.get("artifact_sha256"):
        lines.append(f"artifact_sha256: {result.get('artifact_sha256')}")

    errors = result.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
        lines.append("Errors:")
        for error in errors:
            lines.append(f"  - {error}")

    warnings = result.get("warnings")
    if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines) + "\n"


def _pack_failure(
    *,
    root: Path,
    artifact_path: Path,
    started_at: str,
    status: str,
    validation: Mapping[str, Any],
    metadata_summary: Mapping[str, Any],
    errors: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "package_root": root.as_posix(),
        "artifact_path": artifact_path.as_posix(),
        "artifact_sha256": None,
        "archive_format": "zip",
        "archive_root": _archive_root_name(metadata_summary),
        "metadata": dict(metadata_summary),
        "manifest": None,
        "content_counts": {
            "files": 0,
            "directories": 0,
            "bytes": 0,
            "required_files_checked": len(validation.get("required_files_checked") or []),
            "required_dirs_checked": len(validation.get("required_dirs_checked") or []),
        },
        "validation_status": validation.get("status"),
        "validation": dict(validation),
        "included_files": [],
        "excluded_roots": sorted(PACKAGE_EXCLUDED_ROOTS),
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": list(validation.get("warnings") or []),
        "errors": list(errors),
    }


def _metadata_from_doctor_result(result: Mapping[str, Any]) -> dict[str, Any]:
    checks = result.get("checks")
    if isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        for check in checks:
            if not isinstance(check, Mapping) or check.get("name") != "skill_metadata":
                continue
            metadata = check.get("metadata")
            if isinstance(metadata, Mapping):
                return dict(metadata)
    return {}


def _metadata_summary(metadata: Mapping[str, Any]) -> dict[str, Any]:
    package_roots = metadata.get("package_roots")
    if not isinstance(package_roots, Sequence) or isinstance(package_roots, (str, bytes)):
        package_roots = []
    return {
        "schema_version": str(metadata.get("schema_version") or ""),
        "name": str(metadata.get("name") or "loopplane"),
        "version": str(metadata.get("version") or "0.0.0"),
        "description": str(metadata.get("description") or ""),
        "entrypoint": str(metadata.get("entrypoint") or "SKILL.md"),
        "package_roots": [str(root) for root in package_roots if isinstance(root, str)],
    }


def _archive_root_name(metadata: Mapping[str, Any]) -> str:
    raw_name = str(metadata.get("name") or "loopplane")
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in raw_name).strip(".-_")
    return cleaned or "loopplane"


def _resolve_pack_output(root: Path, output: Path | str | None, metadata: Mapping[str, Any]) -> Path:
    artifact_name = f"{_archive_root_name(metadata)}-{_safe_version(metadata.get('version'))}.zip"
    if output is None:
        return (root / "dist" / artifact_name).resolve()
    output_path = Path(output).expanduser()
    if output_path.exists() and output_path.is_dir():
        return (output_path / artifact_name).resolve()
    return output_path.resolve()


def _safe_version(version: object) -> str:
    raw = str(version or "0.0.0")
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in raw).strip(".-_")
    return cleaned or "0.0.0"


def _collect_package_files(root: Path, metadata: Mapping[str, Any]) -> list[str]:
    package_roots = metadata.get("package_roots")
    if not isinstance(package_roots, Sequence) or isinstance(package_roots, (str, bytes)):
        package_roots = []
    selected_roots = [root_name for root_name in EXPECTED_PACKAGE_ROOTS if root_name in set(package_roots)]
    selected: set[str] = set()

    for relative in PACKAGE_TOP_LEVEL_FILES:
        candidate = root / relative
        if candidate.is_file() and not _is_excluded_package_path(Path(relative)):
            selected.add(relative)

    for root_name in selected_roots:
        root_path = root / root_name
        if not root_path.is_dir():
            continue
        for candidate in root_path.rglob("*"):
            if not candidate.is_file():
                continue
            relative_path = candidate.relative_to(root)
            if _is_excluded_package_path(relative_path):
                continue
            selected.add(relative_path.as_posix())

    return sorted(selected)


def _is_excluded_package_path(relative_path: Path) -> bool:
    if any(part in PACKAGE_EXCLUDED_ROOTS for part in relative_path.parts):
        return True
    name = relative_path.name
    if name in {".DS_Store"}:
        return True
    return any(name.endswith(suffix) for suffix in PACKAGE_EXCLUDED_SUFFIXES)


def _package_content_counts(root: Path, included_files: Sequence[str]) -> dict[str, int]:
    directories: set[str] = set()
    byte_count = 0
    for relative in included_files:
        path = root / relative
        byte_count += path.stat().st_size
        parent = Path(relative).parent
        while parent.as_posix() not in ("", "."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return {
        "files": len(included_files),
        "directories": len(directories),
        "bytes": byte_count,
        "required_files_checked": len(REQUIRED_PACKAGE_FILES),
        "required_dirs_checked": len(REQUIRED_PACKAGE_DIRS),
    }


def _write_package_zip(root: Path, artifact_path: Path, archive_root: str, included_files: Sequence[str]) -> None:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in included_files:
            source = root / relative
            archive_name = f"{archive_root}/{relative}"
            info = zipfile.ZipInfo(archive_name)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (_archive_file_mode(relative) & 0o777) << 16
            archive.writestr(info, source.read_bytes())


def _archive_file_mode(relative: str) -> int:
    return 0o755 if relative in EXECUTABLE_PACKAGE_FILES else 0o644


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_cli_module(script_path: Path, root: Path) -> Any:
    if not script_path.is_file():
        raise FileNotFoundError(script_path)
    module_name = "_loopplane_cli_release_gate_" + sha256(script_path.as_posix().encode("utf-8")).hexdigest()[:12]
    loader = importlib.machinery.SourceFileLoader(module_name, script_path.as_posix())
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise ImportError(f"unable to create import spec for {script_path}")
    module = importlib.util.module_from_spec(spec)
    original_path = list(sys.path)
    previous_module = sys.modules.get(module_name)
    sys.modules[module_name] = module
    if root.as_posix() not in sys.path:
        sys.path.insert(0, root.as_posix())
    try:
        loader.exec_module(module)
    finally:
        sys.path[:] = original_path
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
    return module


def _collect_cli_command_handlers(parser: argparse.ArgumentParser) -> dict[tuple[str, ...], dict[str, Any]]:
    handlers: dict[tuple[str, ...], dict[str, Any]] = {}

    def walk(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    command = (*prefix, str(name))
                    entry = {
                        "handler": subparser.get_default("handler"),
                        "command_path": tuple(subparser.get_default("command_path") or command),
                    }
                    handlers[command] = entry
                    for alias in subparser.get_default("command_aliases") or ():
                        alias_key = tuple(str(part) for part in alias)
                        if alias_key:
                            handlers[alias_key] = dict(entry)
                    walk(subparser, command)

    walk(parser, ())
    return handlers


def _handler_name(handler: Any) -> str | None:
    if handler is None:
        return None
    return getattr(handler, "__name__", repr(handler))


def _command_label(command: Sequence[str] | str) -> str:
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _format_deferred_commands(commands: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    formatted: list[dict[str, str]] = []
    for entry in commands:
        command = entry.get("command")
        if isinstance(command, Sequence) and not isinstance(command, (str, bytes)):
            label = _command_label(command)
        else:
            label = str(command or "")
        profiles = entry.get("profiles")
        if isinstance(profiles, Sequence) and not isinstance(profiles, (str, bytes)) and profiles:
            label = f"{label} --profile {','.join(str(profile) for profile in profiles)}"
        formatted.append(
            {
                "command": label,
                "rationale": str(entry.get("rationale") or ""),
            }
        )
    return formatted


def _release_item_map(items: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        if item_id:
            mapped[item_id] = dict(item)
    return mapped


def _release_problem_message(problem: Mapping[str, Any]) -> str:
    item_id = str(problem.get("id") or "<missing-id>")
    problem_name = str(problem.get("problem") or "classification_problem")
    message = str(problem.get("message") or "")
    return f"{problem_name}: {item_id}" + (f": {message}" if message else "")


def _matching_stale_doc_terms(line: str) -> list[str]:
    matches: list[str] = []
    for entry in STALE_COMPLETION_DOC_PATTERNS:
        pattern = str(entry.get("pattern") or "")
        if pattern and re.search(pattern, line, flags=re.IGNORECASE):
            matches.append(str(entry.get("id") or pattern))
    return matches


def _matching_smoke_risk_patterns(line: str, patterns: Sequence[Mapping[str, Any]]) -> list[str]:
    matches: list[str] = []
    for entry in patterns:
        pattern = str(entry.get("pattern") or "")
        if pattern and re.search(pattern, line, flags=re.IGNORECASE):
            matches.append(str(entry.get("id") or pattern))
    return matches


def _matching_boundary_terms(text: str, terms: Sequence[str]) -> list[str]:
    normalized = _normalize_doc_text(text)
    return [
        str(term)
        for term in terms
        if str(term) and _normalize_doc_text(str(term)) in normalized
    ]


def _normalize_doc_text(text: str) -> str:
    return " ".join(text.lower().split())


def _line_context_text(lines: Sequence[str], index: int, *, window: int) -> str:
    start = max(0, index - window)
    end = min(len(lines), index + window + 1)
    return "\n".join(line.strip() for line in lines[start:end] if line.strip())


def _matching_doc_contexts(text: str, contexts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = " ".join(text.lower().split())
    matches: list[dict[str, Any]] = []
    for entry in contexts:
        aliases = entry.get("aliases", ())
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            aliases = ()
        matched_aliases = [
            str(alias)
            for alias in aliases
            if str(alias) and " ".join(str(alias).lower().split()) in normalized
        ]
        if not matched_aliases:
            continue
        record = {
            "id": str(entry.get("id") or ""),
            "label": str(entry.get("label") or entry.get("id") or ""),
            "matched_aliases": matched_aliases,
        }
        completed_scope = str(entry.get("completed_scope") or "")
        if completed_scope:
            record["completed_scope"] = completed_scope
        matches.append(record)
    return matches


def _package_group_files(group: Mapping[str, Any]) -> tuple[str, ...]:
    raw_files = group.get("files")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
        return ()
    return tuple(str(path) for path in raw_files if str(path))


def _package_group_paths(group: Mapping[str, Any]) -> tuple[str, ...]:
    raw_paths = group.get("paths")
    if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
        return ()
    return tuple(str(path) for path in raw_paths if str(path))


def _required_adapter_methods(entry: Mapping[str, Any]) -> list[str]:
    raw = entry.get("required_methods")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [str(item) for item in raw if str(item)]
    return ["run", "doctor"]


def _adapter_class_base_names(node: ast.ClassDef) -> tuple[str, ...]:
    return tuple(name for base in node.bases if (name := _adapter_expr_name(base)))


def _resolve_adapter_method_owner(
    class_name: str,
    method_name: str,
    class_map: Mapping[str, Mapping[str, Any]],
    seen: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if class_name in seen:
        return None
    class_entry = class_map.get(class_name)
    if class_entry is None:
        return None
    class_node = class_entry.get("node")
    if isinstance(class_node, ast.ClassDef):
        for statement in class_node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == method_name:
                return {
                    "class": class_name,
                    "module": class_entry.get("module"),
                    "method_node": statement,
                }
    for base_name in class_entry.get("bases", ()):
        owner = _resolve_adapter_method_owner(str(base_name), method_name, class_map, seen | {class_name})
        if owner is not None:
            return owner
    return None


def _adapter_method_placeholder_problems(method_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    problems: list[str] = []
    body = _body_without_docstring(method_node.body)
    if not body:
        problems.append("empty_method")
    elif all(_is_placeholder_statement(statement) for statement in body):
        problems.append("placeholder_body")

    for node in ast.walk(method_node):
        if isinstance(node, ast.Raise) and node.exc is not None and _is_notimplemented_expr(node.exc):
            _append_unique(problems, "raises_NotImplementedError")
        elif isinstance(node, ast.Return) and node.value is not None:
            if _is_notimplemented_expr(node.value):
                _append_unique(problems, "returns_NotImplemented")
            elif _is_not_implemented_call(node.value):
                _append_unique(problems, "delegates_to_not_implemented")
        elif isinstance(node, ast.Expr) and _is_not_implemented_call(node.value):
            _append_unique(problems, "delegates_to_not_implemented")
    return problems


def _body_without_docstring(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    statements = list(body)
    if statements and isinstance(statements[0], ast.Expr):
        value = statements[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return statements[1:]
    return statements


def _is_placeholder_statement(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr):
        value = statement.value
        return isinstance(value, ast.Constant) and value.value is Ellipsis
    return False


def _is_notimplemented_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        return _is_notimplemented_expr(node.func)
    name = _adapter_expr_name(node)
    return name in {"NotImplemented", "NotImplementedError"}


def _is_not_implemented_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _adapter_expr_name(node.func)
    return name in {"not_implemented", "_not_implemented"}


def _is_abstractmethod(method_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_adapter_expr_name(decorator) == "abstractmethod" for decorator in method_node.decorator_list)


def _adapter_expr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _adapter_expr_name(node.value)
    if isinstance(node, ast.Call):
        return _adapter_expr_name(node.func)
    return None


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _missing_paths(root: Path, paths: Sequence[str], *, want_file: bool) -> list[str]:
    missing: list[str] = []
    for relative in paths:
        candidate = root / relative
        exists = candidate.is_file() if want_file else candidate.is_dir()
        if not exists:
            missing.append(relative)
    return missing


def _check_executable_files(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in EXECUTABLE_PACKAGE_FILES:
        path = root / relative
        records.append(
            {
                "path": relative,
                "exists": path.is_file(),
                "executable": path.is_file() and os.access(path, os.X_OK),
            }
        )
    return records


def _install_failure(
    *,
    project: Path,
    started_at: str,
    status: str,
    errors: Sequence[str],
    conflicts: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": INSTALL_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": None,
        "workspace_id": None,
        "layout": INSTALL_INSTANCE_PROFILE,
        "workflow_root": ".loopplane",
        "created": [],
        "preserved": [],
        "created_directories": [],
        "schema_validation": None,
        "version_control_status": None,
        "version_control_problem": None,
        "runner_readiness": None,
        "agent_skill_installations": [],
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": [],
        "errors": list(errors),
        "conflicts": list(conflicts),
    }


def _update_failure(
    *,
    project: Path,
    started_at: str,
    status: str,
    errors: Sequence[str],
    conflicts: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema_version": UPDATE_SCHEMA_VERSION,
        "ok": False,
        "status": status,
        "project_root": project.as_posix(),
        "workflow_id": None,
        "workspace_id": None,
        "layout": INSTALL_INSTANCE_PROFILE,
        "workflow_root": ".loopplane",
        "created": [],
        "updated": [],
        "preserved": [],
        "created_directories": [],
        "protected_paths": [],
        "schema_validation": None,
        "version_control_status": None,
        "version_control_problem": None,
        "runner_readiness": None,
        "agent_skill_installations": [],
        "started_at": started_at,
        "ended_at": _utc_now(),
        "warnings": [],
        "errors": list(errors),
        "conflicts": list(conflicts),
    }


def _load_workflow(project: Path) -> dict[str, Any]:
    path = project / ".loopplane" / "config" / "workflow.json"
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise WorkflowPathError(f"{path}: workflow config must be a JSON object")
    workflow_id = data.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise WorkflowPathError(f"{path}: workflow_id must be a non-empty string")
    return data


def _ensure_agent_skill_projections(
    project: Path,
    *,
    mode: str,
    created: list[str],
    updated: list[str] | None,
    preserved: list[str],
    created_directories: list[str],
    targets: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    projection = _agent_skill_projection_source()
    plans: list[dict[str, Any]] = []
    conflicts: list[str] = []
    results: list[dict[str, Any]] = []

    selected_targets = targets if targets is not None else AGENT_SKILL_INSTALL_TARGETS
    for target in selected_targets:
        plan = _plan_agent_skill_projection(project, target, projection, mode=mode)
        plans.append(plan)
        conflicts.extend(str(conflict) for conflict in plan["conflicts"])

    if conflicts:
        if mode == "update":
            raise SkillUpdateConflictError(conflicts)
        raise SkillInstallConflictError(conflicts)

    for plan in plans:
        new_directories = _materialize_agent_skill_projection(
            project,
            plan,
            created=created,
            updated=updated,
            preserved=preserved,
        )
        for directory in new_directories:
            if directory not in created_directories:
                created_directories.append(directory)
        results.append(
            {
                "schema_version": AGENT_SKILL_PROJECTION_SCHEMA_VERSION,
                "agent_style": plan["agent_style"],
                "status": _agent_skill_projection_status(plan),
                "skill_name": projection["skill_name"],
                "skill_root": _relative(plan["skill_root"], project),
                "entrypoint": f"{_relative(plan['skill_root'], project)}/SKILL.md",
                "managed_file_count": len(projection["source_files"]),
                "created_files": plan["new_file_count"],
                "updated_files": plan["updated_file_count"],
                "preserved_files": plan["preserved_file_count"],
            }
        )
    return results


def _preflight_agent_skill_projection_conflicts(
    project: Path,
    *,
    mode: str,
    targets: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    projection = _agent_skill_projection_source()
    conflicts: list[str] = []
    selected_targets = targets if targets is not None else AGENT_SKILL_INSTALL_TARGETS
    for target in selected_targets:
        plan = _plan_agent_skill_projection(project, target, projection, mode=mode)
        conflicts.extend(str(conflict) for conflict in plan["conflicts"])
    if conflicts:
        if mode == "update":
            raise SkillUpdateConflictError(conflicts)
        raise SkillInstallConflictError(conflicts)


def _selected_agent_skill_install_targets(
    agent_styles: Sequence[str] | None,
    *,
    project_agent_skills: bool,
) -> list[Mapping[str, Any]]:
    if not project_agent_skills:
        return []
    if not agent_styles:
        return [dict(target) for target in AGENT_SKILL_INSTALL_TARGETS]
    requested = {_normalize_agent_style(value) for value in agent_styles if str(value).strip()}
    return [dict(target) for target in AGENT_SKILL_INSTALL_TARGETS if str(target["agent_style"]) in requested]


def _normalize_agent_style(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text == "claude":
        return "claude_code"
    return text


def _agent_skill_projection_status(plan: Mapping[str, Any]) -> str:
    if int(plan.get("updated_file_count") or 0):
        return "updated"
    if int(plan.get("new_file_count") or 0):
        return "created"
    return "preserved"


def _agent_skill_projection_source() -> dict[str, Any]:
    metadata, metadata_errors, _metadata_warnings = _check_metadata(PACKAGE_ROOT)
    if metadata_errors:
        raise SkillInstallConflictError([f"{PACKAGE_ROOT / 'skill.json'}: {error}" for error in metadata_errors])

    compatibility = _check_agent_skill_entrypoint_compatibility(PACKAGE_ROOT, metadata)
    if compatibility["status"] != "pass":
        entrypoint = str(metadata.get("entrypoint") or "SKILL.md")
        raise SkillInstallConflictError(
            [f"{PACKAGE_ROOT / entrypoint}: {error}" for error in compatibility["errors"]]
        )

    source_files = tuple(_collect_package_files(PACKAGE_ROOT, metadata))
    if not source_files:
        raise SkillInstallConflictError([f"{PACKAGE_ROOT}: no package files selected for agent skill projection"])

    file_bytes = {relative: (PACKAGE_ROOT / relative).read_bytes() for relative in source_files}
    file_hashes = {
        relative: sha256(content).hexdigest()
        for relative, content in file_bytes.items()
    }
    return {
        "metadata": metadata,
        "skill_name": str(metadata.get("name") or ""),
        "source_files": source_files,
        "file_bytes": file_bytes,
        "file_hashes": file_hashes,
    }


def _plan_agent_skill_projection(
    project: Path,
    target: Mapping[str, Any],
    projection: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    skill_name = str(projection["skill_name"])
    skill_root = project / str(target["root"]) / skill_name
    manifest_relative = ".loopplane_projection.json"
    source_files = tuple(str(path) for path in projection["source_files"])
    file_bytes = projection["file_bytes"]
    if not isinstance(file_bytes, Mapping):
        raise SkillInstallConflictError(["agent skill projection file_bytes must be a mapping"])
    file_payloads: dict[str, bytes] = {relative: bytes(file_bytes[relative]) for relative in source_files}
    file_payloads[manifest_relative] = _json_bytes(
        _agent_skill_projection_manifest(
            agent_style=str(target["agent_style"]),
            projection=projection,
            managed_files=source_files,
        )
    )

    conflicts: list[str] = []
    new_file_count = 0
    updated_file_count = 0
    preserved_file_count = 0

    for directory in _parents_between(project, skill_root):
        if directory.exists() and not directory.is_dir():
            conflicts.append(f"{directory}: exists and is not a directory")
    if skill_root.exists() and not skill_root.is_dir():
        conflicts.append(f"{skill_root}: exists and is not a directory")

    existing_manifest = _read_projection_manifest(skill_root)
    for relative, content in file_payloads.items():
        destination = skill_root / relative
        for parent in _parents_between(skill_root, destination.parent):
            if parent.exists() and not parent.is_dir():
                conflicts.append(f"{parent}: exists and is not a directory")
        if not destination.exists():
            new_file_count += 1
            continue
        if not destination.is_file():
            conflicts.append(f"{destination}: exists and is not a regular file")
            continue
        existing = destination.read_bytes()
        if existing == content:
            preserved_file_count += 1
            continue
        if (
            mode == "update"
            and relative == manifest_relative
            and existing_manifest.get("schema_version") == AGENT_SKILL_PROJECTION_SCHEMA_VERSION
        ):
            updated_file_count += 1
            continue
        if mode == "update" and _projection_manifest_allows_update(existing_manifest, relative, existing):
            updated_file_count += 1
            continue
        conflicts.append(f"{destination}: exists with different content")

    return {
        "agent_style": str(target["agent_style"]),
        "skill_root": skill_root,
        "file_payloads": file_payloads,
        "conflicts": conflicts,
        "new_file_count": new_file_count,
        "updated_file_count": updated_file_count,
        "preserved_file_count": preserved_file_count,
    }


def _materialize_agent_skill_projection(
    project: Path,
    plan: Mapping[str, Any],
    *,
    created: list[str],
    updated: list[str] | None,
    preserved: list[str],
) -> list[str]:
    skill_root = plan["skill_root"]
    if not isinstance(skill_root, Path):
        raise SkillInstallConflictError(["agent skill projection skill_root must be a path"])
    payloads = plan["file_payloads"]
    if not isinstance(payloads, Mapping):
        raise SkillInstallConflictError(["agent skill projection payloads must be a mapping"])

    created_directories: list[str] = []
    for relative, content in payloads.items():
        relative_path = Path(str(relative))
        destination = skill_root / relative_path
        for directory in _parents_between(project, destination.parent):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created_directories.append(_relative(directory, project))

        existed = destination.exists()
        same = existed and destination.is_file() and destination.read_bytes() == content
        if same:
            preserved.append(_relative(destination, project))
            _ensure_projected_file_mode(destination, str(relative_path))
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        _ensure_projected_file_mode(destination, str(relative_path))
        if existed and updated is not None:
            updated.append(_relative(destination, project))
        else:
            created.append(_relative(destination, project))
    return created_directories


def _agent_skill_projection_manifest(
    *,
    agent_style: str,
    projection: Mapping[str, Any],
    managed_files: Sequence[str],
) -> dict[str, Any]:
    metadata = projection["metadata"]
    if not isinstance(metadata, Mapping):
        metadata = {}
    file_hashes = projection["file_hashes"]
    if not isinstance(file_hashes, Mapping):
        file_hashes = {}
    return {
        "schema_version": AGENT_SKILL_PROJECTION_SCHEMA_VERSION,
        "agent_style": agent_style,
        "skill_name": str(projection["skill_name"]),
        "package_name": str(metadata.get("name") or ""),
        "package_version": str(metadata.get("version") or ""),
        "package_metadata_schema_version": str(metadata.get("schema_version") or ""),
        "entrypoint": str(metadata.get("entrypoint") or "SKILL.md"),
        "managed_files": list(managed_files),
        "managed_file_sha256": {str(relative): str(file_hashes.get(relative) or "") for relative in managed_files},
    }


def _read_projection_manifest(skill_root: Path) -> dict[str, Any]:
    manifest = skill_root / ".loopplane_projection.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _projection_manifest_allows_update(
    manifest: Mapping[str, Any],
    relative: str,
    existing: bytes,
) -> bool:
    if manifest.get("schema_version") != AGENT_SKILL_PROJECTION_SCHEMA_VERSION:
        return False
    managed_hashes = manifest.get("managed_file_sha256")
    if not isinstance(managed_hashes, Mapping):
        return False
    previous_hash = managed_hashes.get(relative)
    if not isinstance(previous_hash, str) or not previous_hash:
        return False
    return sha256(existing).hexdigest() == previous_hash


def _parents_between(root: Path, leaf: Path) -> list[Path]:
    root = root.resolve()
    leaf = leaf.resolve()
    parents: list[Path] = []
    current = leaf
    while True:
        try:
            current.relative_to(root)
        except ValueError:
            break
        parents.append(current)
        if current == root:
            break
        current = current.parent
    return list(reversed(parents))


def _ensure_projected_file_mode(path: Path, relative: str) -> None:
    if relative in EXECUTABLE_PACKAGE_FILES:
        path.chmod(path.stat().st_mode | 0o755)


def _check_agent_skill_entrypoint_compatibility(
    root: Path,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    entrypoint = str(metadata.get("entrypoint") or "SKILL.md")
    path = root / entrypoint
    errors: list[str] = []
    frontmatter = _read_skill_frontmatter(path)
    name = str(frontmatter.get("name") or "")
    description = str(frontmatter.get("description") or "")
    metadata_name = str(metadata.get("name") or "")

    if not frontmatter:
        errors.append("SKILL.md must start with YAML frontmatter delimited by ---")
    if not name:
        errors.append("SKILL.md frontmatter is missing required name")
    elif not _valid_agent_skill_name(name):
        errors.append(f"SKILL.md frontmatter name {name!r} is not a portable Agent Skills name")
    if metadata_name and name and metadata_name != name:
        errors.append(f"SKILL.md frontmatter name {name!r} does not match skill.json name {metadata_name!r}")
    if not description:
        errors.append("SKILL.md frontmatter is missing required description")
    elif len(description) > 1024:
        errors.append("SKILL.md frontmatter description exceeds 1024 characters")

    return {
        "name": "agent_skill_entrypoint_compatibility",
        "status": "pass" if not errors else "fail",
        "entrypoint": entrypoint,
        "frontmatter": frontmatter,
        "expected_skill_directory": metadata_name,
        "codex_skill_root": f".codex/skills/{metadata_name}" if metadata_name else "",
        "claude_code_skill_root": f".claude/skills/{metadata_name}" if metadata_name else "",
        "errors": errors,
    }


def _read_skill_frontmatter(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    frontmatter_lines: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        frontmatter_lines.append(line)
    else:
        return {}

    data: dict[str, str] = {}
    for line in frontmatter_lines:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            data[key] = value
    return data


def _valid_agent_skill_name(name: str) -> bool:
    return bool(AGENT_SKILL_NAME_RE.fullmatch(name)) and "--" not in name


def _install_runner_readiness(project: Path, *, auto_configure: bool) -> dict[str, Any]:
    discovery: list[dict[str, Any]] = []
    configured: dict[str, Any] | None = None
    try:
        config = load_agent_runners(project)
    except (AgentRunnerConfigError, OSError, json.JSONDecodeError, WorkflowPathError) as error:
        return {
            "schema_version": RUNNER_READINESS_SCHEMA_VERSION,
            "ok": False,
            "status": "waiting_config",
            "project_root": project.as_posix(),
            "required_runner_ids": [],
            "auto_configure": auto_configure,
            "discovery": discovery,
            "configured_runner_ids": [],
            "local_override_path": None,
            "doctor": None,
            "errors": [f"Unable to load agent runner configuration: {error}"],
            "next_steps": _runner_readiness_next_steps(project, list(INSTALL_TIME_DEFAULT_ADAPTER_ORDER)),
        }

    install_cli_runners = _install_cli_runners(config.runners.values())
    required_runners = _install_required_cli_runners(config.runners.values())
    if auto_configure:
        configured = _auto_configure_install_cli_runners(
            project,
            install_cli_runners,
            discovery,
            default_runner_id=config.default_runner,
        )
        if configured.get("configured_runner_ids"):
            try:
                config = load_agent_runners(project)
                install_cli_runners = _install_cli_runners(config.runners.values())
                required_runners = _install_required_cli_runners(config.runners.values())
            except (AgentRunnerConfigError, OSError, json.JSONDecodeError, WorkflowPathError) as error:
                return {
                    "schema_version": RUNNER_READINESS_SCHEMA_VERSION,
                    "ok": False,
                    "status": "waiting_config",
                    "project_root": project.as_posix(),
                    "required_runner_ids": [runner.runner_id for runner in required_runners],
                    "auto_configure": auto_configure,
                    "discovery": discovery,
                    "configured_runner_ids": list(configured.get("configured_runner_ids", [])),
                    "local_override_path": configured.get("local_override_path"),
                    "doctor": None,
                    "errors": [f"Unable to reload agent runner configuration after CLI discovery: {error}"],
                    "next_steps": _runner_readiness_next_steps(project, _adapters_for_runners(required_runners)),
                }

    doctor_results: list[dict[str, Any]] = []
    doctor_errors: list[str] = []
    for runner in required_runners:
        result = doctor_agent_runners(project, runner_id=runner.runner_id)
        runner_results = result.get("runner_results")
        if isinstance(runner_results, list):
            doctor_results.extend(dict(item) for item in runner_results if isinstance(item, Mapping))
        if not result.get("ok"):
            errors = result.get("errors")
            if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
                doctor_errors.extend(str(error) for error in errors)
            elif result.get("message"):
                doctor_errors.append(str(result["message"]))

    missing = [
        item
        for item in discovery
        if item.get("required") is True and item.get("status") == "missing"
    ]
    for item in missing:
        program = item.get("program") or "CLI"
        adapter = item.get("adapter") or "runner"
        doctor_errors.append(f"{adapter}: required install-time CLI program {program!r} was not found on PATH or known editor extension directories")

    ok = not doctor_errors and all(result.get("status") == "ok" for result in doctor_results)
    missing_optional_adapters = sorted(
        {
            str(item.get("adapter"))
            for item in discovery
            if item.get("status") == "missing"
            and item.get("required") is not True
            and item.get("adapter") in INSTALL_TIME_CLI_ADAPTERS
        }
    )
    next_steps = (
        _runner_readiness_next_steps(project, missing_optional_adapters)
        if ok and missing_optional_adapters
        else ([] if ok else _runner_readiness_next_steps(project, _adapters_for_runners(required_runners)))
    )
    return {
        "schema_version": RUNNER_READINESS_SCHEMA_VERSION,
        "ok": ok,
        "status": "ok" if ok else "waiting_config",
        "project_root": project.as_posix(),
        "required_runner_ids": [runner.runner_id for runner in required_runners],
        "auto_configure": auto_configure,
        "discovery": discovery,
        "configured_runner_ids": list((configured or {}).get("configured_runner_ids", [])),
        "local_override_path": (configured or {}).get("local_override_path"),
        "doctor": {
            "status": "ok" if ok else "waiting_config",
            "runner_results": doctor_results,
        },
        "errors": _dedupe_strings(doctor_errors),
        "next_steps": next_steps,
    }


def _install_required_cli_runners(runners: Sequence[RunnerConfig] | Any) -> list[RunnerConfig]:
    selected = [
        runner
        for runner in runners
        if runner.enabled and runner.adapter in INSTALL_TIME_CLI_ADAPTERS
    ]
    return sorted(selected, key=lambda runner: runner.runner_id)


def _install_cli_runners(runners: Sequence[RunnerConfig] | Any) -> list[RunnerConfig]:
    selected = [
        runner
        for runner in runners
        if runner.adapter in INSTALL_TIME_CLI_ADAPTERS
    ]
    return sorted(selected, key=lambda runner: runner.runner_id)


def _auto_configure_install_cli_runners(
    project: Path,
    runners: Sequence[RunnerConfig],
    discovery: list[dict[str, Any]],
    *,
    default_runner_id: str,
) -> dict[str, Any]:
    overrides: dict[str, Mapping[str, Any]] = {}
    discovered_by_adapter: dict[str, Path | None] = {}
    for adapter in INSTALL_TIME_DEFAULT_ADAPTER_ORDER:
        program = INSTALL_TIME_CLI_ADAPTERS[adapter]
        discovered = _discover_install_cli_program(program)
        discovered_by_adapter[adapter] = discovered

    default_adapter = _install_default_adapter(discovered_by_adapter)
    for adapter in INSTALL_TIME_DEFAULT_ADAPTER_ORDER:
        program = INSTALL_TIME_CLI_ADAPTERS[adapter]
        discovered = discovered_by_adapter.get(adapter)
        discovery.append(
            {
                "adapter": adapter,
                "program": program,
                "status": "found" if discovered else "missing",
                "path": discovered.as_posix() if discovered else None,
                "required": default_adapter is None or adapter == default_adapter,
            }
        )

    if default_adapter is None:
        return {"configured_runner_ids": [], "local_override_path": None, "default_adapter": None}

    runner_by_id = {runner.runner_id: runner for runner in runners}
    default_runner = runner_by_id.get(default_runner_id) or _first_install_worker_runner(runners)
    default_discovered = discovered_by_adapter.get(default_adapter)
    if default_runner is not None and default_discovered is not None:
        default_command = _install_runner_command(
            default_adapter,
            default_discovered,
        )
        overrides[default_runner.runner_id] = _install_runner_override(
            default_runner,
            default_command,
            adapter=default_adapter,
            enabled=True,
        )

    for adapter, discovered in discovered_by_adapter.items():
        if not discovered:
            continue
        if adapter == default_adapter:
            continue
        command = _install_runner_command(adapter, discovered)
        for runner in runners:
            if runner.adapter != adapter:
                continue
            if runner.inherits or runner.role != "worker":
                continue
            if default_runner is not None and runner.runner_id == default_runner.runner_id:
                continue
            if _runner_command_is_absolute(runner.command) and runner.enabled:
                continue
            overrides[runner.runner_id] = _install_runner_override(
                runner,
                command,
                adapter=adapter,
                enabled=True,
            )

    if not overrides:
        return {"configured_runner_ids": [], "local_override_path": None}
    try:
        write_result = write_project_local_agent_runner_overrides(project, overrides)
    except (OSError, AgentRunnerConfigError) as error:
        discovery.append(
            {
                "adapter": "project_local_override",
                "program": "agent_runners.local.json",
                "status": "failed",
                "path": None,
                "required": True,
                "error": str(error),
            }
        )
        return {"configured_runner_ids": [], "local_override_path": None}
    return {
        "configured_runner_ids": list(write_result.get("runner_ids", [])),
        "local_override_path": str(write_result.get("path") or ""),
        "default_adapter": default_adapter,
    }


def _install_runner_command(adapter: str, discovered: Path) -> str:
    if adapter == "codex_cli":
        return "codex"
    return shlex.quote(discovered.as_posix())


def _install_default_adapter(discovered_by_adapter: Mapping[str, Path | None]) -> str | None:
    for adapter in INSTALL_TIME_DEFAULT_ADAPTER_ORDER:
        if discovered_by_adapter.get(adapter) is not None:
            return adapter
    return None


def _first_install_worker_runner(runners: Sequence[RunnerConfig]) -> RunnerConfig | None:
    enabled_workers = [runner for runner in runners if runner.role == "worker" and runner.enabled and not runner.inherits]
    if enabled_workers:
        return sorted(enabled_workers, key=lambda runner: runner.runner_id)[0]
    workers = [runner for runner in runners if runner.role == "worker" and not runner.inherits]
    if workers:
        return sorted(workers, key=lambda runner: runner.runner_id)[0]
    return None


def _discover_install_cli_program(program: str) -> Path | None:
    if program == "codex":
        resolution = resolve_codex_executable(
            "codex",
            env=os.environ,
            cwd=Path.cwd(),
        )
        return Path(resolution.resolved_path) if resolution.resolved_path is not None else None

    discovered = shutil.which(program)
    if discovered:
        return Path(discovered).resolve()

    candidates: list[Path] = []
    for root in _install_cli_extension_roots():
        if not root.is_dir():
            continue
        try:
            for candidate in root.rglob(program):
                if _install_cli_candidate_is_file(candidate):
                    candidates.append(candidate.resolve())
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=_install_cli_candidate_sort_key)


def _install_cli_extension_roots() -> list[Path]:
    home = Path.home()
    return [home / relative for relative in INSTALL_TIME_CLI_EXTENSION_ROOTS]


def _install_cli_candidate_is_file(candidate: Path) -> bool:
    try:
        return candidate.is_file()
    except OSError:
        return False


def _install_cli_candidate_sort_key(candidate: Path) -> tuple[float, str]:
    try:
        mtime = candidate.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (mtime, candidate.as_posix())


def _install_runner_override(
    runner: RunnerConfig,
    command: str,
    *,
    adapter: str | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    adapter_value = adapter or runner.adapter
    doctor = dict(runner.doctor)
    doctor["check_command"] = f"{command} --version"
    doctor["requires_auth"] = True
    if adapter_value == "codex_cli":
        doctor["auth_check_command"] = f"{command} login status"
        doctor.pop("check_auth_command", None)
    elif adapter_value == "claude_code_cli":
        doctor["auth_check_command"] = f"{command} auth status"
        doctor.pop("check_auth_command", None)
    return {
        "role": runner.role,
        "adapter": adapter_value,
        "command": command,
        "prompt_delivery": _install_prompt_delivery(adapter_value, runner.prompt_delivery),
        "enabled": runner.enabled if enabled is None else enabled,
        "doctor": doctor,
    }


def _install_prompt_delivery(adapter: str, current: Mapping[str, Any]) -> dict[str, Any]:
    if adapter == "codex_cli":
        return {
            "mode": "file_argument",
            "argument_template": "{{prompt_path}}",
        }
    if adapter == "claude_code_cli":
        return {
            "mode": "stdin_or_prompt_flag",
            "prompt_file": "{{prompt_path}}",
        }
    return dict(current)


def _runner_command_is_absolute(command: str) -> bool:
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return bool(parts and Path(parts[0]).expanduser().is_absolute())


def _adapters_for_runners(runners: Sequence[RunnerConfig]) -> list[str]:
    return sorted({runner.adapter for runner in runners if runner.adapter in INSTALL_TIME_CLI_ADAPTERS})


def _runner_readiness_next_steps(project: Path, adapters: Sequence[str]) -> list[str]:
    steps: list[str] = []
    if not adapters or "codex_cli" in adapters:
        steps.append(
            "Configure the stable Codex CLI command before planning: "
            f"python3 scripts/loopplane configure-agent --project {shlex.quote(project.as_posix())} "
            "--runner worker --role worker --adapter codex_cli --command codex && "
            f"python3 scripts/loopplane doctor-agent --project {shlex.quote(project.as_posix())} --runner worker"
        )
    if "claude_code_cli" in adapters:
        steps.append(
            "Find and configure Claude Code CLI to enable worker failover: "
            f"CLAUDE_BIN=$(command -v claude) && python3 scripts/loopplane configure-agent --project {shlex.quote(project.as_posix())} "
            '--runner worker_fallback --role worker --adapter claude_code_cli --command "$CLAUDE_BIN" && '
            f"python3 scripts/loopplane doctor-agent --project {shlex.quote(project.as_posix())} --runner worker_fallback"
        )
    steps.append(f"Run python3 scripts/loopplane doctor-agent --project {shlex.quote(project.as_posix())} --all after configuring every runner you expect to use.")
    return _dedupe_strings(steps)


def _dedupe_strings(items: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _preflight_new_install_metadata(project: Path) -> None:
    conflicts: list[str] = []
    metadata_files = (
        project / ".loopplane" / "workspace.json",
        project / ".loopplane" / "workflow_registry.json",
        project / ".loopplane" / "current_workflow.json",
        project / ".loopplane" / "config" / "instance.json",
        project / ".loopplane" / "config" / "workflow_defaults.json",
        project / ".loopplane" / "config" / "local" / ".gitignore",
    )
    for path in metadata_files:
        if path.exists():
            conflicts.append(f"{path}: install metadata exists without .loopplane/config/workflow.json")

    metadata_dirs = (
        project / ".loopplane",
        project / ".loopplane" / "config",
        project / ".loopplane" / "config" / "local",
    )
    for directory in metadata_dirs:
        if directory.exists() and not directory.is_dir():
            conflicts.append(f"{directory}: exists and is not a directory")
    if conflicts:
        raise SkillInstallConflictError(conflicts)


def _ensure_install_directories(project: Path, paths: WorkflowPaths) -> list[str]:
    directories = [
        project / ".loopplane",
        project / ".loopplane" / "config",
        project / ".loopplane" / "config" / "local",
        project / ".loopplane" / "prompts",
        paths.planning_dir,
        paths.planning_dir / "runs",
        paths.runtime_dir,
        paths.runtime_dir / "lock",
        paths.runtime_dir / "lock" / "scheduler_instance_lock",
        paths.runtime_dir / "lock" / "event_append_lock",
        paths.runtime_dir / "events",
        paths.runtime_dir / "snapshots",
        paths.runtime_dir / "runs",
        paths.runtime_dir / "active_run_leases",
        paths.runtime_dir / "migrations",
        paths.requests_dir,
        paths.read_models_dir,
        paths.results_dir,
    ]
    created: list[str] = []
    conflicts: list[str] = []
    for directory in directories:
        if directory.exists():
            if not directory.is_dir():
                conflicts.append(f"{directory}: exists and is not a directory")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        created.append(_relative(directory, project))
    if conflicts:
        raise SkillInstallConflictError(conflicts)
    return created


def _ensure_workspace_identity(
    project: Path,
    workflow: Mapping[str, Any],
    installed_at: str,
    created: list[str],
    preserved: list[str],
) -> _InstallWorkspace:
    workspace_file = project / ".loopplane" / "workspace.json"
    if workspace_file.exists():
        data = _read_json_object(workspace_file)
        workspace_id = data.get("workspace_id")
        created_at = data.get("created_at")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise SkillInstallConflictError([f"{workspace_file}: workspace_id must be a non-empty string"])
        if not isinstance(created_at, str) or not created_at:
            created_at = installed_at
        relative = _relative(workspace_file, project)
        if relative not in created:
            preserved.append(relative)
        return _InstallWorkspace(workspace_id=workspace_id, created_at=created_at, existing=True)

    workspace = _InstallWorkspace(
        workspace_id=_new_workspace_id(),
        created_at=installed_at,
        existing=False,
    )
    payload = {
        "schema_version": "1.6",
        "workspace_id": workspace.workspace_id,
        "project_root": ".",
        "loopplane_dir": ".loopplane",
        "repo_root": repository_root_value(project),
        "created_at": workspace.created_at,
        "created_by_loopplane_version": INSTALL_TOOL_VERSION,
        "workspace_boundary": "project_root",
        "allow_out_of_boundary_writes": False,
        "single_active_running_workflow": True,
        "layout": INSTALL_INSTANCE_PROFILE,
        "current_workflow_id": workflow["workflow_id"],
    }
    _write_new_json(workspace_file, payload, project, created, preserved)
    return workspace


def _ensure_install_metadata_files(
    *,
    project: Path,
    workflow: Mapping[str, Any],
    paths: WorkflowPaths,
    workspace: _InstallWorkspace,
    installed_at: str,
    created: list[str],
    preserved: list[str],
) -> None:
    workflow_id = str(workflow["workflow_id"])
    registry_file = project / ".loopplane" / "workflow_registry.json"
    current_file = project / ".loopplane" / "current_workflow.json"
    instance_file = project / ".loopplane" / "config" / "instance.json"
    defaults_file = project / ".loopplane" / "config" / "workflow_defaults.json"
    local_gitignore = project / ".loopplane" / "config" / "local" / ".gitignore"
    layout, workflow_root = _install_layout_for_paths(paths)

    _ensure_current_pointer(
        current_file,
        project,
        workspace=workspace,
        workflow_id=workflow_id,
        installed_at=installed_at,
        created=created,
        preserved=preserved,
    )
    _ensure_workflow_registry(
        registry_file,
        project,
        workspace=workspace,
        workflow=workflow,
        paths=paths,
        installed_at=installed_at,
        created=created,
        preserved=preserved,
    )

    instance = {
        "schema_version": "1.6",
        "workspace_id": workspace.workspace_id,
        "current_workflow_id": workflow_id,
        "installed_at": installed_at,
        "installed_by": INSTALL_TOOL_VERSION,
        "layout": layout,
        "workflow_root": workflow_root,
        "project_root": ".",
    }
    _write_new_json(instance_file, instance, project, created, preserved, preserve_existing_json_object=True)

    defaults = {
        "schema_version": "1.6",
        "layout": layout,
        "workflow_root": workflow_root,
        "brief_file": paths.value("brief_file"),
        "plan_file": paths.value("plan_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "planning_dir": paths.value("planning_dir"),
        "runtime_dir": paths.value("runtime_dir"),
        "read_models_dir": paths.value("read_models_dir"),
        "requests_dir": paths.value("requests_dir"),
        "results_dir": paths.value("results_dir"),
        "version_control_config_file": paths.value("version_control_config_file"),
    }
    _write_new_json(defaults_file, defaults, project, created, preserved, preserve_existing_json_object=True)
    _write_new_text(local_gitignore, "*\n!.gitignore\n", project, created, preserved)


def _ensure_update_metadata_files(
    *,
    project: Path,
    workflow: Mapping[str, Any],
    paths: WorkflowPaths,
    workspace: _InstallWorkspace,
    updated_at: str,
    created: list[str],
    updated: list[str],
    preserved: list[str],
) -> None:
    workflow_id = str(workflow["workflow_id"])
    registry_file = project / ".loopplane" / "workflow_registry.json"
    current_file = project / ".loopplane" / "current_workflow.json"
    instance_file = project / ".loopplane" / "config" / "instance.json"
    defaults_file = project / ".loopplane" / "config" / "workflow_defaults.json"
    local_gitignore = project / ".loopplane" / "config" / "local" / ".gitignore"
    package_manifest_file = project / ".loopplane" / "config" / "package_manifest.json"
    layout, workflow_root = _install_layout_for_paths(paths)

    _ensure_current_pointer(
        current_file,
        project,
        workspace=workspace,
        workflow_id=workflow_id,
        installed_at=updated_at,
        created=created,
        preserved=preserved,
        selection_reason="skill_update",
    )
    _ensure_workflow_registry(
        registry_file,
        project,
        workspace=workspace,
        workflow=workflow,
        paths=paths,
        installed_at=updated_at,
        created=created,
        preserved=preserved,
    )

    instance = {
        "schema_version": "1.6",
        "workspace_id": workspace.workspace_id,
        "current_workflow_id": workflow_id,
        "installed_at": str(workflow.get("created_at") or updated_at),
        "installed_by": INSTALL_TOOL_VERSION,
        "layout": layout,
        "workflow_root": workflow_root,
        "project_root": ".",
    }
    _write_managed_json(
        instance_file,
        instance,
        project,
        created,
        updated,
        preserved,
        managed_schema_version="1.6",
    )

    defaults = {
        "schema_version": "1.6",
        "layout": layout,
        "workflow_root": workflow_root,
        "brief_file": paths.value("brief_file"),
        "plan_file": paths.value("plan_file"),
        "shared_context_file": paths.value("shared_context_file"),
        "planning_dir": paths.value("planning_dir"),
        "runtime_dir": paths.value("runtime_dir"),
        "read_models_dir": paths.value("read_models_dir"),
        "requests_dir": paths.value("requests_dir"),
        "results_dir": paths.value("results_dir"),
        "version_control_config_file": paths.value("version_control_config_file"),
    }
    _write_managed_json(
        defaults_file,
        defaults,
        project,
        created,
        updated,
        preserved,
        managed_schema_version="1.6",
    )
    _ensure_local_gitignore_for_update(local_gitignore, project, created, preserved)
    _write_project_package_manifest(package_manifest_file, project, workflow, paths, created, updated, preserved)


def _ensure_current_pointer(
    path: Path,
    project: Path,
    *,
    workspace: _InstallWorkspace,
    workflow_id: str,
    installed_at: str,
    created: list[str],
    preserved: list[str],
    selection_reason: str = "skill_install",
) -> None:
    payload = {
        "schema_version": "1.6",
        "workspace_id": workspace.workspace_id,
        "current_workflow_id": workflow_id,
        "selection_reason": selection_reason,
        "updated_at": installed_at,
        "updated_by": INSTALL_TOOL_VERSION,
    }
    if not path.exists():
        _write_new_json(path, payload, project, created, preserved)
        return
    data = _read_json_object(path)
    existing_workflow = data.get("current_workflow_id")
    if existing_workflow != workflow_id:
        raise SkillInstallConflictError(
            [f"{path}: current_workflow_id {existing_workflow!r} does not match {workflow_id!r}"]
        )
    relative = _relative(path, project)
    if relative not in created:
        preserved.append(relative)


def _ensure_workflow_registry(
    path: Path,
    project: Path,
    *,
    workspace: _InstallWorkspace,
    workflow: Mapping[str, Any],
    paths: WorkflowPaths,
    installed_at: str,
    created: list[str],
    preserved: list[str],
) -> None:
    record = _workflow_registry_record(workflow, paths, installed_at)
    if not path.exists():
        payload = {
            "schema_version": "1.6",
            "workspace_id": workspace.workspace_id,
            "generated_at": installed_at,
            "workflows": [record],
        }
        _write_new_json(path, payload, project, created, preserved)
        return

    data = _read_json_object(path)
    workflows = data.get("workflows")
    if not isinstance(workflows, Sequence) or isinstance(workflows, (str, bytes)):
        raise SkillInstallConflictError([f"{path}: workflows must be a list"])
    workflow_id = str(workflow["workflow_id"])
    workflow_root = paths.workflow_root_value.rstrip("/")
    allowed_workflow_roots = {workflow_root, f"{workflow_root}/"}
    for existing in workflows:
        if isinstance(existing, Mapping) and existing.get("workflow_id") == workflow_id:
            if existing.get("workflow_root") not in allowed_workflow_roots:
                raise SkillInstallConflictError(
                    [
                        f"{path}: workflow {workflow_id} uses unsupported workflow_root "
                        f"{existing.get('workflow_root')!r}"
                    ]
                )
            relative = _relative(path, project)
            if relative not in created:
                preserved.append(relative)
            return
    raise SkillInstallConflictError([f"{path}: missing workflow registry entry for {workflow_id}"])


def _workflow_registry_record(workflow: Mapping[str, Any], paths: WorkflowPaths, installed_at: str) -> dict[str, Any]:
    workflow_id = str(workflow["workflow_id"])
    created_at = workflow.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        created_at = installed_at
    return {
        "workflow_id": workflow_id,
        "name": "default workflow",
        "status": "draft",
        "workflow_root": ".loopplane",
        "created_at": created_at,
        "last_seen_at": installed_at,
        "plan_file": paths.value("plan_file"),
        "read_models_dir": paths.value("read_models_dir"),
        "completion_marker": f"{paths.value('runtime_dir')}/plan_loop_complete.json",
        "read_only": False,
        "archived": False,
        "summary": {
            "one_line": "LoopPlane workflow initialized by skill install.",
            "tasks_total": 0,
            "tasks_completed": 0,
            "tasks_blocked": 0,
        },
    }


def _ensure_local_gitignore_for_update(
    path: Path,
    project: Path,
    created: list[str],
    preserved: list[str],
) -> None:
    if path.exists():
        if not path.is_file():
            raise SkillInstallConflictError([f"{path}: exists and is not a regular file"])
        preserved.append(_relative(path, project))
        return
    _write_new_text(path, "*\n!.gitignore\n", project, created, preserved)


def _write_project_package_manifest(
    path: Path,
    project: Path,
    workflow: Mapping[str, Any],
    paths: WorkflowPaths,
    created: list[str],
    updated: list[str],
    preserved: list[str],
) -> None:
    metadata, metadata_errors, _metadata_warnings = _check_metadata(PACKAGE_ROOT)
    if metadata_errors:
        raise SkillUpdateConflictError([f"{PACKAGE_ROOT / 'skill.json'}: {error}" for error in metadata_errors])
    layout, workflow_root = _install_layout_for_paths(paths)

    payload = {
        "schema_version": PROJECT_PACKAGE_MANIFEST_SCHEMA_VERSION,
        "package_name": str(metadata.get("name") or "loopplane"),
        "package_version": str(metadata.get("version") or ""),
        "package_metadata_schema_version": str(metadata.get("schema_version") or ""),
        "runtime_version": RUNTIME_VERSION,
        "tool_version": INSTALL_TOOL_VERSION,
        "layout": layout,
        "workflow_root": workflow_root,
        "project_root": ".",
        "workflow_id": str(workflow.get("workflow_id") or ""),
        "package_roots": sorted(str(item) for item in metadata.get("package_roots", []) if isinstance(item, str)),
        "project_managed_files": _project_package_managed_files(),
        "protected_project_paths": _protected_project_paths(paths),
    }
    _write_managed_json(
        path,
        payload,
        project,
        created,
        updated,
        preserved,
        managed_schema_version=PROJECT_PACKAGE_MANIFEST_SCHEMA_VERSION,
    )


def _install_layout_for_paths(paths: WorkflowPaths) -> tuple[str, str]:
    workflow_root = paths.workflow_root_value.rstrip("/") or ".loopplane"
    if workflow_root == ".loopplane":
        return INSTALL_INSTANCE_PROFILE, workflow_root
    return "canonical_v16", workflow_root


def _write_managed_json(
    path: Path,
    payload: Mapping[str, Any],
    project: Path,
    created: list[str],
    updated: list[str],
    preserved: list[str],
    *,
    managed_schema_version: str,
) -> None:
    content = _json_bytes(payload)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(content)
        created.append(_relative(path, project))
        return

    if not path.is_file():
        raise SkillUpdateConflictError([f"{path}: exists and is not a regular file"])
    try:
        existing = _read_json_object(path)
    except (OSError, json.JSONDecodeError, SkillInstallConflictError) as error:
        raise SkillUpdateConflictError([f"{path}: existing package-managed metadata is not readable JSON: {error}"]) from error
    if existing.get("schema_version") != managed_schema_version:
        raise SkillUpdateConflictError(
            [
                f"{path}: schema_version {existing.get('schema_version')!r} is not "
                f"{managed_schema_version!r}; refusing to overwrite possible local metadata"
            ]
        )

    if path.read_bytes() == content:
        preserved.append(_relative(path, project))
        return
    path.write_bytes(content)
    updated.append(_relative(path, project))


def _project_package_managed_files() -> list[str]:
    return [
        ".loopplane/workspace.json",
        ".loopplane/workflow_registry.json",
        ".loopplane/current_workflow.json",
        ".loopplane/config/instance.json",
        ".loopplane/config/workflow_defaults.json",
        ".loopplane/config/package_manifest.json",
        ".loopplane/config/local/.gitignore",
    ]


def _protected_project_paths(paths: WorkflowPaths) -> list[str]:
    return [
        paths.value("brief_file"),
        paths.value("plan_file"),
        paths.value("shared_context_file"),
        ".loopplane/config/workflow.json",
        ".loopplane/config/security.json",
        ".loopplane/config/dashboard.json",
        ".loopplane/config/agent_runners.json",
        paths.value("version_control_config_file"),
        ".loopplane/config/schema_version.json",
        ".loopplane/config/local",
        paths.value("planning_dir"),
        paths.value("runtime_dir"),
        f"{paths.value('runtime_dir')}/state.json",
        f"{paths.value('runtime_dir')}/events",
        f"{paths.value('runtime_dir')}/snapshots",
        f"{paths.value('runtime_dir')}/runs",
        f"{paths.value('runtime_dir')}/git_checkpoints.jsonl",
        f"{paths.value('runtime_dir')}/control_requests.jsonl",
        f"{paths.value('runtime_dir')}/control_responses.jsonl",
        f"{paths.value('runtime_dir')}/human_approval_requests.jsonl",
        f"{paths.value('runtime_dir')}/human_approval_responses.jsonl",
        f"{paths.value('runtime_dir')}/evidence_manifest.json",
        paths.value("read_models_dir"),
        paths.value("requests_dir"),
        paths.value("results_dir"),
    ]


def _runtime_version_control_status(paths: WorkflowPaths) -> str | None:
    data = _read_optional_json_object(paths.read_models_dir / "version_control_status.json")
    status = data.get("status") if isinstance(data, Mapping) else None
    return str(status) if isinstance(status, str) else None


def _runtime_version_control_problem(paths: WorkflowPaths) -> str | None:
    data = _read_optional_json_object(paths.read_models_dir / "version_control_status.json")
    problem = data.get("problem") if isinstance(data, Mapping) else None
    if isinstance(problem, Mapping) and isinstance(problem.get("code"), str):
        return str(problem["code"])
    return None


def _check_metadata(root: Path) -> tuple[dict[str, Any], list[str], list[str]]:
    path = root / "skill.json"
    errors: list[str] = []
    warnings: list[str] = []
    metadata: dict[str, Any] = {}

    if not path.is_file():
        return metadata, ["skill.json is missing"], warnings
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return metadata, [f"skill.json is not valid JSON: {error}"], warnings
    if not isinstance(data, dict):
        return metadata, ["skill.json must contain a JSON object"], warnings
    metadata = dict(data)

    for key in ("schema_version", "name", "version", "description", "entrypoint", "package_roots"):
        if key not in metadata:
            errors.append(f"skill.json is missing required key: {key}")
    if metadata.get("schema_version") != PACKAGE_METADATA_SCHEMA_VERSION:
        errors.append(
            "skill.json schema_version must be "
            f"{PACKAGE_METADATA_SCHEMA_VERSION!r}, got {metadata.get('schema_version')!r}"
        )

    entrypoint = metadata.get("entrypoint")
    if isinstance(entrypoint, str):
        if not (root / entrypoint).is_file():
            errors.append(f"skill.json entrypoint does not exist: {entrypoint}")
    elif entrypoint is not None:
        errors.append("skill.json entrypoint must be a string")

    package_roots = metadata.get("package_roots")
    if not isinstance(package_roots, list) or not all(isinstance(item, str) for item in package_roots):
        errors.append("skill.json package_roots must be a list of strings")
    else:
        missing_roots = [root_name for root_name in EXPECTED_PACKAGE_ROOTS if root_name not in package_roots]
        if missing_roots:
            errors.append(f"skill.json package_roots is missing: {', '.join(missing_roots)}")
        extra_roots = sorted(set(package_roots).difference(EXPECTED_PACKAGE_ROOTS))
        if extra_roots:
            warnings.append(f"skill.json package_roots contains extra root(s): {', '.join(extra_roots)}")

    return metadata, errors, warnings


def _write_new_json(
    path: Path,
    payload: Mapping[str, Any],
    project: Path,
    created: list[str],
    preserved: list[str],
    *,
    preserve_existing_json_object: bool = False,
) -> None:
    content = _json_bytes(payload)
    if preserve_existing_json_object and path.exists():
        _read_json_object(path)
        preserved.append(_relative(path, project))
        return
    _write_new_bytes(path, content, project, created, preserved)


def _write_new_text(path: Path, text: str, project: Path, created: list[str], preserved: list[str]) -> None:
    _write_new_bytes(path, text.encode("utf-8"), project, created, preserved)


def _write_new_bytes(path: Path, content: bytes, project: Path, created: list[str], preserved: list[str]) -> None:
    if path.exists():
        if not path.is_file():
            raise SkillInstallConflictError([f"{path}: exists and is not a regular file"])
        if path.read_bytes() != content:
            raise SkillInstallConflictError([f"{path}: exists with different content"])
        preserved.append(_relative(path, project))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
    created.append(_relative(path, project))


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SkillInstallConflictError([f"{path}: expected a JSON object"])
    return data


def _read_optional_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return _read_json_object(path)
    except (OSError, json.JSONDecodeError, SkillInstallConflictError):
        return {}


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode("utf-8")


def _relative(path: Path, project: Path) -> str:
    return path.relative_to(project).as_posix()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_workspace_id() -> str:
    return f"ws_{uuid.uuid4().hex}"


def _empty_required_text_files(root: Path, missing_files: Sequence[str]) -> list[str]:
    missing = set(missing_files)
    empty: list[str] = []
    for relative in REQUIRED_PACKAGE_FILES:
        if relative in missing:
            continue
        path = root / relative
        try:
            if path.stat().st_size == 0:
                empty.append(relative)
        except OSError:
            empty.append(relative)
    return empty
