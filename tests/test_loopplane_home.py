from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime.loopplane_home import (
    LOOPPLANE_HOME_DEFAULT,
    LOOPPLANE_HOME_ENV,
    loopplane_home_layout,
    loopplane_home_summary,
    ensure_loopplane_home_layout,
    resolve_loopplane_home,
)


class LoopPlaneHomeResolverTest(unittest.TestCase):
    def test_resolve_loopplane_home_defaults_to_user_loopplane_directory(self) -> None:
        expected = Path(LOOPPLANE_HOME_DEFAULT).expanduser().resolve()

        resolved = resolve_loopplane_home(env={})
        layout = loopplane_home_layout(env={})
        summary = loopplane_home_summary(env={})

        self.assertEqual(resolved, expected)
        self.assertEqual(layout.home, expected)
        self.assertEqual(layout.workspace_registry_file, expected / "registry" / "workspaces.json")
        self.assertEqual(layout.agent_runners_local_file, expected / "runners" / "agent_runners.local.json")
        self.assertEqual(layout.dashboard_servers_file, expected / "dashboard" / "servers.json")
        self.assertEqual(layout.runner_locks_dir, expected / "locks" / "runner_locks")
        self.assertEqual(layout.dashboard_locks_dir, expected / "locks" / "dashboard_locks")
        self.assertEqual(summary["environment_variable"], LOOPPLANE_HOME_ENV)
        self.assertEqual(summary["default"], LOOPPLANE_HOME_DEFAULT)
        self.assertEqual(summary["home"], expected.as_posix())

    def test_resolve_loopplane_home_honors_environment_and_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_home = root / "from-env"
            explicit_home = root / "explicit"

            self.assertEqual(
                resolve_loopplane_home(env={LOOPPLANE_HOME_ENV: str(env_home)}),
                env_home.resolve(),
            )
            self.assertEqual(
                loopplane_home_layout(env={LOOPPLANE_HOME_ENV: str(env_home)}).workspace_registry_file,
                env_home.resolve() / "registry" / "workspaces.json",
            )
            self.assertEqual(
                resolve_loopplane_home(explicit_home, env={LOOPPLANE_HOME_ENV: str(env_home)}),
                explicit_home.resolve(),
            )

    def test_ensure_loopplane_home_layout_creates_recommended_layout_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "loopplane-home"
            layout = loopplane_home_layout(home)

            first = ensure_loopplane_home_layout(home)

            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "ready")
            for path in (
                layout.home,
                layout.registry_dir,
                layout.runners_dir,
                layout.dashboard_dir,
                layout.locks_dir,
                layout.runner_locks_dir,
                layout.dashboard_locks_dir,
                layout.package_cache_dir,
                layout.logs_dir,
            ):
                self.assertTrue(path.is_dir(), path)
            self.assertEqual(
                _read_json(layout.config_file),
                {"authority": "discovery_only", "schema_version": "1.6"},
            )
            self.assertEqual(
                _read_json(layout.workspace_registry_file),
                {"authority": "discovery_only", "schema_version": "1.6", "workspaces": []},
            )
            self.assertEqual(
                _read_json(layout.agent_runners_local_file),
                {"schema_version": "1.6", "runners": {}},
            )
            self.assertEqual(
                _read_json(layout.dashboard_servers_file),
                {"schema_version": "1.6", "servers": []},
            )
            before = {
                path: path.read_bytes()
                for path in (
                    layout.config_file,
                    layout.workspace_registry_file,
                    layout.agent_runners_local_file,
                    layout.dashboard_servers_file,
                )
            }

            second = ensure_loopplane_home_layout(home)

            self.assertEqual(second["created"], [])
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_ensure_loopplane_home_layout_preserves_existing_machine_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "loopplane-home"
            layout = loopplane_home_layout(home)
            existing_payloads = {
                layout.config_file: {"schema_version": "1.6", "custom": True},
                layout.workspace_registry_file: {
                    "schema_version": "1.6",
                    "workspaces": [{"workspace_id": "ws_existing_workspace"}],
                },
                layout.agent_runners_local_file: {
                    "schema_version": "1.6",
                    "runners": {"codex_worker": {"command": "codex"}},
                },
                layout.dashboard_servers_file: {
                    "schema_version": "1.6",
                    "servers": [{"url": "http://127.0.0.1:3766"}],
                },
            }
            for path, payload in existing_payloads.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            ensure_loopplane_home_layout(home)

            for path, payload in existing_payloads.items():
                self.assertEqual(_read_json(path), payload)
            self.assertTrue(layout.runner_locks_dir.is_dir())
            self.assertTrue(layout.dashboard_locks_dir.is_dir())
            self.assertTrue(layout.package_cache_dir.is_dir())
            self.assertTrue(layout.logs_dir.is_dir())


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
