from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence


OBJECTIVE_LINE_RE = re.compile(
    r"^- \[(?P<status>[ x~!\-])\]\s+`(?P<objective_id>[A-Za-z0-9_.-]+)`\s+(?P<text>.+?)\s*$"
)
OBJECTIVE_FIELD_LINE_RE = re.compile(r"^  - (?P<field>[A-Za-z0-9_ -]+):(?P<value>.*)$")
PHASE_HEADING_RE = re.compile(r"^## Phase\b")
PHASE_OBJECTIVE_HEADING_RE = re.compile(r"^###\s+Phase Objective Checklist\s*$", re.IGNORECASE)
FINAL_OBJECTIVE_HEADING_RE = re.compile(r"^##\s+Final Objective Checklist\s*$", re.IGNORECASE)

DEFAULT_OBJECTIVE_MAX_EXPANSIONS = 100


@dataclass(frozen=True)
class ObjectiveRecord:
    objective_id: str
    status: str
    text: str
    scope: str
    phase_id: str | None
    phase_title: str | None
    fields: Mapping[str, tuple[str, ...]]
    line_index: int
    block: str

    @property
    def status_label(self) -> str:
        return f"[{self.status}]"

    @property
    def status_name(self) -> str:
        return {
            " ": "unchecked",
            "x": "satisfied",
            "~": "partial",
            "!": "exceptional_unresolved",
            "-": "waived",
        }.get(self.status, "unknown")

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "objective_id": self.objective_id,
            "status": self.status_name,
            "checkbox": self.status_label,
            "text": self.text,
            "scope": self.scope,
            "fields": {key: list(value) for key, value in self.fields.items()},
            "line_index": self.line_index,
        }
        if self.phase_id:
            data["phase_id"] = self.phase_id
        if self.phase_title:
            data["phase_title"] = self.phase_title
        for key in (
            "evidence_scope",
            "judgment_guidance",
            "verifier",
            "unmet_action",
            "risk",
            "max_expansions",
            "linked_tasks",
            "followup_tasks",
            "followup_phases",
        ):
            values = self.fields.get(key)
            if values:
                data[key] = values[0] if len(values) == 1 else list(values)
        return data


def is_objective_heading(line: str) -> bool:
    return PHASE_OBJECTIVE_HEADING_RE.match(line) is not None or FINAL_OBJECTIVE_HEADING_RE.match(line) is not None


def is_objective_line(line: str) -> bool:
    return OBJECTIVE_LINE_RE.match(line) is not None


def is_task_block_terminator(line: str) -> bool:
    return is_objective_heading(line) or is_objective_line(line)


def parse_plan_objectives(plan_text: str) -> tuple[list[ObjectiveRecord], list[str]]:
    lines = plan_text.splitlines()
    objectives: list[ObjectiveRecord] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    current_phase_title: str | None = None
    scope: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if PHASE_HEADING_RE.match(line):
            current_phase_title = line[3:].strip()
            scope = None
            index += 1
            continue
        if PHASE_OBJECTIVE_HEADING_RE.match(line):
            scope = "phase"
            index += 1
            continue
        if FINAL_OBJECTIVE_HEADING_RE.match(line):
            scope = "workflow"
            current_phase_title = None
            index += 1
            continue
        match = OBJECTIVE_LINE_RE.match(line)
        if not match:
            index += 1
            continue
        objective_id = match.group("objective_id").strip()
        if scope not in {"phase", "workflow"}:
            errors.append(f"Objective {objective_id!r} appears outside an objective checklist.")
            objective_scope = "unknown"
        else:
            objective_scope = scope
        if objective_id in seen:
            errors.append(
                f"Duplicate objective id {objective_id!r} at line {index + 1}; first seen at line {seen[objective_id] + 1}."
            )
        else:
            seen[objective_id] = index
        start = index
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if OBJECTIVE_LINE_RE.match(candidate) or PHASE_OBJECTIVE_HEADING_RE.match(candidate) or FINAL_OBJECTIVE_HEADING_RE.match(candidate) or PHASE_HEADING_RE.match(candidate):
                break
            index += 1
        fields = _objective_fields(lines[start:index])
        phase_title = current_phase_title if objective_scope == "phase" else None
        objective = ObjectiveRecord(
            objective_id=objective_id,
            status=match.group("status"),
            text=match.group("text").strip(),
            scope=objective_scope,
            phase_id=_phase_id(phase_title) if phase_title else None,
            phase_title=phase_title,
            fields=fields,
            line_index=start,
            block="\n".join(lines[start:index]).rstrip(),
        )
        objectives.append(objective)
    return objectives, errors


def objective_closure_fingerprint(
    plan_text: str,
    *,
    project_root: Path | None = None,
    report_paths: Mapping[str, str] | None = None,
) -> str:
    objectives, errors = parse_plan_objectives(plan_text)
    reports = dict(report_paths or {})
    payload = {
        "parse_errors": errors,
        "objectives": [
            {
                "objective_id": objective.objective_id,
                "scope": objective.scope,
                "phase_id": objective.phase_id,
                "status": objective.status,
                "text": objective.text,
                "report_path": reports.get(objective.objective_id),
                "report_sha256": _sha256_path(project_root, reports.get(objective.objective_id)),
            }
            for objective in objectives
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def objective_structure_fingerprint(
    plan_text: str = "",
    *,
    objectives: Sequence[ObjectiveRecord] | None = None,
    scope: str | None = None,
    phase_id: str | None = None,
) -> str:
    """Hash objective identity and guidance while ignoring closure checkboxes."""
    if objectives is None:
        selected, errors = parse_plan_objectives(plan_text)
    else:
        selected = list(objectives)
        errors = []
    if scope:
        selected = [objective for objective in selected if objective.scope == scope]
    if phase_id is not None:
        selected = [objective for objective in selected if objective.phase_id == phase_id]
    payload = {
        "schema": "loopplane-objective-structure-v1",
        "parse_errors": errors,
        "objectives": [
            {
                "objective_id": objective.objective_id,
                "scope": objective.scope,
                "phase_id": objective.phase_id,
                "phase_title": objective.phase_title,
                "text": objective.text,
                "fields": {key: list(values) for key, values in sorted(objective.fields.items())},
            }
            for objective in selected
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


def _objective_fields(lines: list[str]) -> dict[str, tuple[str, ...]]:
    fields: dict[str, list[str]] = {}
    current_field: str | None = None
    for line in lines[1:]:
        match = OBJECTIVE_FIELD_LINE_RE.match(line)
        if match:
            field = _canonical_field(match.group("field"))
            fields.setdefault(field, []).append(match.group("value").strip())
            current_field = field
            continue
        if current_field is not None and (line.startswith("    ") or line.startswith("\t")):
            continuation = line.strip()
            if continuation:
                values = fields.setdefault(current_field, [])
                if values:
                    values[-1] = f"{values[-1]}\n{continuation}" if values[-1] else continuation
    return {key: tuple(value) for key, value in fields.items()}


def _canonical_field(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _phase_id(phase_title: str | None) -> str | None:
    if not phase_title:
        return None
    match = re.match(r"^Phase\s+([^:]+)", phase_title)
    if match:
        return match.group(1).strip()
    return phase_title.strip()


def _sha256_path(project_root: Path | None, value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    try:
        return "sha256:" + sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
