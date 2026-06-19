from __future__ import annotations

import json
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EXCERPT_CHARS = 1200


def path_for_record(project_root: Path | None, path: Path | None) -> str | None:
    if path is None:
        return None
    resolved = Path(path).expanduser()
    if project_root is not None and not resolved.is_absolute():
        resolved = project_root / resolved
    if project_root is None:
        return resolved.as_posix()
    try:
        return resolved.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def file_reference(
    project_root: Path | None,
    path: Path,
    *,
    label: str,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    if project_root is not None and not resolved.is_absolute():
        resolved = project_root / resolved
    record: dict[str, Any] = {
        "label": label,
        "path": path_for_record(project_root, resolved),
        "exists": resolved.exists(),
    }
    if not resolved.exists() or not resolved.is_file():
        return record
    try:
        raw = resolved.read_bytes()
    except OSError as error:
        record["read_error"] = str(error)
        return record
    record["size_bytes"] = len(raw)
    record["sha256"] = "sha256:" + sha256(raw).hexdigest()
    if excerpt_chars > 0:
        sample = raw[: max(excerpt_chars * 4, excerpt_chars)]
        text = sample.decode("utf-8", errors="replace")
        record["excerpt"] = text[:excerpt_chars]
        record["excerpt_truncated"] = len(text) > excerpt_chars or len(raw) > len(sample)
    return record


def data_reference(
    project_root: Path | None,
    path: Path,
    data: Any,
    *,
    label: str,
    excerpt_chars: int = DEFAULT_EXCERPT_CHARS,
) -> dict[str, Any]:
    write_json_file(path, data)
    return file_reference(project_root, path, label=label, excerpt_chars=excerpt_chars)


def prompt_reference_index(references: Mapping[str, Any]) -> dict[str, Any]:
    index: dict[str, Any] = {}
    for key, value in references.items():
        if isinstance(value, Mapping):
            index[str(key)] = {
                "label": str(value.get("label") or key),
                "path": str(value.get("path") or ""),
            }
        else:
            index[str(key)] = {"label": str(key), "path": str(value)}
    return index


def slim_references(references: Mapping[str, Any]) -> dict[str, Any]:
    return prompt_reference_index(references)


def json_summary(value: Any, *, max_chars: int = 1600) -> str:
    text = json.dumps(_json_safe(value), indent=2, sort_keys=True)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...<truncated>"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
