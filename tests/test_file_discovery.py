from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runtime.file_discovery import discover_files_bounded


class BoundedFileDiscoveryTest(unittest.TestCase):
    def test_prunes_cache_trees_before_enumerating_their_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache" / "nested"
            cache.mkdir(parents=True)
            for index in range(500):
                (cache / f"node_summary_{index}.json").write_text("{}\n", encoding="utf-8")
            visible = root / "run_1" / "node_summary.json"
            visible.parent.mkdir()
            visible.write_text("{}\n", encoding="utf-8")

            result = discover_files_bounded((root,), names={"node_summary.json"}, max_entries=20)

            self.assertEqual(result.paths, (visible,))
            self.assertFalse(result.truncated)
            self.assertLess(result.scanned_entries, 10)

    def test_stops_at_entry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(20):
                (root / f"file_{index:02d}.json").write_text("{}\n", encoding="utf-8")

            result = discover_files_bounded((root,), max_entries=5, max_matches=100)

            self.assertTrue(result.truncated)
            self.assertEqual(result.limit_reason, "max_entries")
            self.assertLessEqual(len(result.paths), 5)


if __name__ == "__main__":
    unittest.main()
