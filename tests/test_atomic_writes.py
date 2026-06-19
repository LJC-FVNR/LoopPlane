from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime import prompt_builder, read_models, reconciliation


def temp_artifacts(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.*.tmp"))


class AtomicWriteUtilityTest(unittest.TestCase):
    def test_text_atomic_writer_replaces_existing_file_and_removes_temp_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "artifact.txt"
            target.parent.mkdir(parents=True)
            target.write_text("old\n", encoding="utf-8")

            prompt_builder._atomic_write_text(target, "new\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(temp_artifacts(target), [])

    def test_json_atomic_writers_create_parent_directories_and_stable_json(self) -> None:
        writers = (
            prompt_builder._atomic_write_json,
            reconciliation._atomic_write_json,
            read_models._atomic_write_json,
        )
        for writer in writers:
            with self.subTest(writer=writer.__module__):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "missing" / "record.json"

                    writer(target, {"z": 1, "a": {"b": True}})

                    self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": {"b": True}, "z": 1})
                    self.assertTrue(target.read_text(encoding="utf-8").endswith("\n"))
                    self.assertEqual(temp_artifacts(target), [])

    def test_jsonl_atomic_writer_replaces_file_with_compact_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "models" / "feed.jsonl"
            target.parent.mkdir(parents=True)
            target.write_text('{"stale":true}\n', encoding="utf-8")

            read_models._atomic_write_jsonl(target, [{"event": "one", "seq": 1}, {"event": "two", "seq": 2}])

            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line) for line in lines], [{"event": "one", "seq": 1}, {"event": "two", "seq": 2}])
            self.assertEqual(lines, ['{"event":"one","seq":1}', '{"event":"two","seq":2}'])
            self.assertEqual(temp_artifacts(target), [])


if __name__ == "__main__":
    unittest.main()
