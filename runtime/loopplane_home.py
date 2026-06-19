from __future__ import annotations

import os
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping
from typing import Any


LOOPPLANE_HOME_ENV = "LOOPPLANE_HOME"
LOOPPLANE_HOME_DEFAULT = "~/.loopplane"
LOOPPLANE_HOME_SCHEMA_VERSION = "1.6"
LOOPPLANE_HOME_AUTHORITY = "discovery_only"


@dataclass(frozen=True)
class LoopPlaneHomeLayout:
    home: Path
    config_file: Path
    registry_dir: Path
    workspace_registry_file: Path
    runners_dir: Path
    agent_runners_local_file: Path
    dashboard_dir: Path
    dashboard_servers_file: Path
    locks_dir: Path
    runner_locks_dir: Path
    dashboard_locks_dir: Path
    package_cache_dir: Path
    logs_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "home": self.home.as_posix(),
            "config_file": self.config_file.as_posix(),
            "registry_dir": self.registry_dir.as_posix(),
            "workspace_registry_file": self.workspace_registry_file.as_posix(),
            "runners_dir": self.runners_dir.as_posix(),
            "agent_runners_local_file": self.agent_runners_local_file.as_posix(),
            "dashboard_dir": self.dashboard_dir.as_posix(),
            "dashboard_servers_file": self.dashboard_servers_file.as_posix(),
            "locks_dir": self.locks_dir.as_posix(),
            "runner_locks_dir": self.runner_locks_dir.as_posix(),
            "dashboard_locks_dir": self.dashboard_locks_dir.as_posix(),
            "package_cache_dir": self.package_cache_dir.as_posix(),
            "logs_dir": self.logs_dir.as_posix(),
        }


def resolve_loopplane_home(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the machine-local LOOPPLANE_HOME directory.

    Explicit values take precedence over the environment. When neither is set,
    the LoopPlane v1.6 default is `~/.loopplane`.
    """

    environ = os.environ if env is None else env
    raw = str(value or "").strip()
    if not raw:
        raw = str(environ.get(LOOPPLANE_HOME_ENV) or "").strip()
    if not raw:
        raw = LOOPPLANE_HOME_DEFAULT
    return Path(raw).expanduser().resolve()


def loopplane_home_layout(value: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> LoopPlaneHomeLayout:
    home = resolve_loopplane_home(value, env=env)
    registry_dir = home / "registry"
    runners_dir = home / "runners"
    dashboard_dir = home / "dashboard"
    locks_dir = home / "locks"
    return LoopPlaneHomeLayout(
        home=home,
        config_file=home / "config.json",
        registry_dir=registry_dir,
        workspace_registry_file=registry_dir / "workspaces.json",
        runners_dir=runners_dir,
        agent_runners_local_file=runners_dir / "agent_runners.local.json",
        dashboard_dir=dashboard_dir,
        dashboard_servers_file=dashboard_dir / "servers.json",
        locks_dir=locks_dir,
        runner_locks_dir=locks_dir / "runner_locks",
        dashboard_locks_dir=locks_dir / "dashboard_locks",
        package_cache_dir=home / "package_cache",
        logs_dir=home / "logs",
    )


def loopplane_home_summary(value: Path | str | None = None, *, env: Mapping[str, str] | None = None) -> dict[str, str]:
    layout = loopplane_home_layout(value, env=env)
    return {
        "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
        "environment_variable": LOOPPLANE_HOME_ENV,
        "default": LOOPPLANE_HOME_DEFAULT,
        **layout.as_dict(),
    }


def ensure_loopplane_home_layout(
    value: Path | str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create the recommended LOOPPLANE_HOME layout without overwriting local state."""

    layout = loopplane_home_layout(value, env=env)
    created: list[str] = []
    existing: list[str] = []

    for key, path in _layout_directories(layout):
        if path.exists():
            existing.append(key)
        else:
            path.mkdir(parents=True, exist_ok=True)
            created.append(key)

    for key, path, payload in _layout_files(layout):
        if path.exists():
            existing.append(key)
            continue
        _atomic_write_json(path, payload)
        created.append(key)

    return {
        "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
        "ok": True,
        "status": "ready",
        "authority": LOOPPLANE_HOME_AUTHORITY,
        "layout": layout.as_dict(),
        "created": created,
        "existing": existing,
    }


def _layout_directories(layout: LoopPlaneHomeLayout) -> tuple[tuple[str, Path], ...]:
    return (
        ("home", layout.home),
        ("registry_dir", layout.registry_dir),
        ("runners_dir", layout.runners_dir),
        ("dashboard_dir", layout.dashboard_dir),
        ("locks_dir", layout.locks_dir),
        ("runner_locks_dir", layout.runner_locks_dir),
        ("dashboard_locks_dir", layout.dashboard_locks_dir),
        ("package_cache_dir", layout.package_cache_dir),
        ("logs_dir", layout.logs_dir),
    )


def _layout_files(layout: LoopPlaneHomeLayout) -> tuple[tuple[str, Path, Mapping[str, Any]], ...]:
    return (
        (
            "config_file",
            layout.config_file,
            {
                "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
                "authority": LOOPPLANE_HOME_AUTHORITY,
            },
        ),
        (
            "workspace_registry_file",
            layout.workspace_registry_file,
            {
                "authority": LOOPPLANE_HOME_AUTHORITY,
                "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
                "workspaces": [],
            },
        ),
        (
            "agent_runners_local_file",
            layout.agent_runners_local_file,
            {
                "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
                "runners": {},
            },
        ),
        (
            "dashboard_servers_file",
            layout.dashboard_servers_file,
            {
                "schema_version": LOOPPLANE_HOME_SCHEMA_VERSION,
                "servers": [],
            },
        ),
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
