from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SPECIFIC_PATTERN = re.compile(
    r"(?:slurm|sbatch|squeue|sacct|sinfo|scontrol|nvidia-smi|--gres|gres/|\bgpu\b|\bcuda\b|\baccelerator\b)",
    flags=re.IGNORECASE,
)


class BackendNeutralityTest(unittest.TestCase):
    def test_core_runtime_and_prompts_do_not_embed_site_backend_commands(self) -> None:
        paths = [
            REPOSITORY_ROOT / "SKILL.md",
            REPOSITORY_ROOT / "scripts" / "loopplane",
            *sorted((REPOSITORY_ROOT / "runtime").rglob("*.py")),
            *sorted((REPOSITORY_ROOT / "runtime").rglob("*.md")),
            *sorted((REPOSITORY_ROOT / "runtime" / "schemas").rglob("*.json")),
            *sorted((REPOSITORY_ROOT / "templates").rglob("*.md")),
        ]
        violations: list[str] = []
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = BACKEND_SPECIFIC_PATTERN.search(line)
                if match:
                    violations.append(
                        f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}: {match.group(0)}"
                    )

        self.assertEqual(
            violations,
            [],
            "LoopPlane core must remain execution-backend neutral. Put site-specific "
            "instructions in project shared context, runner configuration, or optional skills.",
        )


if __name__ == "__main__":
    unittest.main()
