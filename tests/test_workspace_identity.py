from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.workspace_identity import discover_enclosing_git_root, repository_root_value


class WorkspaceIdentityGitDiscoveryTest(unittest.TestCase):
    def test_malformed_ancestor_git_directory_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            project = root / "nested" / "project"
            project.mkdir(parents=True)

            self.assertIsNone(discover_enclosing_git_root(project))
            self.assertEqual(repository_root_value(project), ".")

    def test_initialized_ancestor_repository_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git_dir = root / ".git"
            git_dir.mkdir()
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            project = root / "nested" / "project"
            project.mkdir(parents=True)

            self.assertEqual(discover_enclosing_git_root(project), root.resolve())
            self.assertEqual(repository_root_value(project), "../..")


if __name__ == "__main__":
    unittest.main()
