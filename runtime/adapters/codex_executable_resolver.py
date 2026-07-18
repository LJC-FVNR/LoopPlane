from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


CODEX_BINARY_OVERRIDE_ENV = "LOOPPLANE_CODEX_BIN"
_CODEX_PROGRAM_NAME = "codex"
_DEFAULT_EXTENSION_ROOTS = (
    ".vscode-server/extensions",
    ".vscode-server-insiders/extensions",
    ".vscode/extensions",
)


@dataclass(frozen=True)
class ExecutableResolution:
    configured_program: str
    invocation_program: str
    resolved_path: str | None
    source: str
    recovered: bool

    @property
    def available(self) -> bool:
        return self.resolved_path is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "configured_program": self.configured_program,
            "invocation_program": self.invocation_program,
            "resolved_path": self.resolved_path,
            "source": self.source,
            "recovered": self.recovered,
        }


def resolve_codex_executable(
    configured_program: str,
    *,
    env: Mapping[str, str],
    cwd: str | Path,
    extension_roots: Sequence[Path | str] | None = None,
) -> ExecutableResolution:
    """Resolve Codex without pinning LoopPlane to a versioned editor extension.

    Existing explicit commands retain precedence unless LOOPPLANE_CODEX_BIN is
    set. Automatic fallback is deliberately limited to commands actually named
    ``codex`` so a missing arbitrary executable is never silently replaced.
    """

    configured = configured_program.strip()
    configured_path = _resolve_program(configured, env=env, cwd=cwd)
    if not _is_codex_program(configured):
        return _resolution_for_configured_program(configured, configured_path)

    override = str(env.get(CODEX_BINARY_OVERRIDE_ENV, "")).strip()
    if override:
        override_path = _resolve_program(override, env=env, cwd=cwd)
        if override_path is not None:
            return ExecutableResolution(
                configured_program=configured,
                invocation_program=override_path,
                resolved_path=override_path,
                source="environment_override",
                recovered=configured_path is None,
            )

    if configured_path is not None:
        return ExecutableResolution(
            configured_program=configured,
            invocation_program=configured if not _has_path_component(configured) else configured_path,
            resolved_path=configured_path,
            source="path" if not _has_path_component(configured) else "configured_path",
            recovered=False,
        )

    path_codex = shutil.which(_CODEX_PROGRAM_NAME, path=env.get("PATH"))
    if path_codex is not None:
        resolved = _normalized_executable(Path(path_codex))
        if resolved is not None:
            return ExecutableResolution(
                configured_program=configured,
                invocation_program=resolved,
                resolved_path=resolved,
                source="path_fallback",
                recovered=True,
            )

    extension_codex = _newest_extension_codex(
        configured_program=configured,
        env=env,
        cwd=cwd,
        extension_roots=extension_roots,
    )
    if extension_codex is not None:
        return ExecutableResolution(
            configured_program=configured,
            invocation_program=extension_codex,
            resolved_path=extension_codex,
            source="vscode_extension",
            recovered=True,
        )

    return ExecutableResolution(
        configured_program=configured,
        invocation_program=configured,
        resolved_path=None,
        source="unresolved",
        recovered=False,
    )


def _resolution_for_configured_program(
    configured_program: str,
    resolved_path: str | None,
) -> ExecutableResolution:
    return ExecutableResolution(
        configured_program=configured_program,
        invocation_program=configured_program,
        resolved_path=resolved_path,
        source="configured_command" if resolved_path is not None else "unresolved",
        recovered=False,
    )


def _resolve_program(
    program: str,
    *,
    env: Mapping[str, str],
    cwd: str | Path,
) -> str | None:
    if not program:
        return None
    if not _has_path_component(program):
        located = shutil.which(program, path=env.get("PATH"))
        return _normalized_executable(Path(located)) if located is not None else None

    candidate = Path(program).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return _normalized_executable(candidate)


def _newest_extension_codex(
    *,
    configured_program: str,
    env: Mapping[str, str],
    cwd: str | Path,
    extension_roots: Sequence[Path | str] | None,
) -> str | None:
    candidates: list[tuple[tuple[int, ...], int, str]] = []
    for root in _extension_roots(
        env,
        extension_roots,
        configured_program=configured_program,
        cwd=cwd,
    ):
        for pattern in (
            "openai.chatgpt-*/bin/*/codex",
            "openai.chatgpt-*/bin/codex",
        ):
            try:
                discovered = root.glob(pattern)
            except OSError:
                continue
            for candidate in discovered:
                normalized = _normalized_executable(candidate)
                if normalized is None:
                    continue
                try:
                    modified_ns = candidate.stat().st_mtime_ns
                except OSError:
                    continue
                candidates.append((_extension_version(candidate), modified_ns, normalized))
    if not candidates:
        return None
    return max(candidates)[2]


def _extension_roots(
    env: Mapping[str, str],
    configured_roots: Sequence[Path | str] | None,
    *,
    configured_program: str,
    cwd: str | Path,
) -> tuple[Path, ...]:
    if configured_roots is not None:
        return tuple(Path(root).expanduser() for root in configured_roots)

    roots: list[Path] = []
    home_text = str(env.get("HOME", "")).strip()
    if home_text:
        home = Path(home_text).expanduser()
        roots.extend(home / relative for relative in _DEFAULT_EXTENSION_ROOTS)
    vscode_agent_folder = str(env.get("VSCODE_AGENT_FOLDER", "")).strip()
    if vscode_agent_folder:
        roots.append(Path(vscode_agent_folder).expanduser() / "extensions")
    inferred_root = _configured_extension_root(configured_program, cwd=cwd)
    if inferred_root is not None:
        roots.append(inferred_root)

    unique: dict[str, Path] = {}
    for root in roots:
        unique.setdefault(root.as_posix(), root)
    return tuple(unique.values())


def _configured_extension_root(program: str, *, cwd: str | Path) -> Path | None:
    candidate = Path(program).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    for parent in candidate.parents:
        if parent.name.startswith("openai.chatgpt-") and parent.parent.name == "extensions":
            return parent.parent
    return None


def _extension_version(candidate: Path) -> tuple[int, ...]:
    for parent in candidate.parents:
        if not parent.name.startswith("openai.chatgpt-"):
            continue
        version_text = parent.name.removeprefix("openai.chatgpt-")
        match = re.match(r"(\d+(?:\.\d+)*)", version_text)
        if match is not None:
            return tuple(int(part) for part in match.group(1).split("."))
        break
    return ()


def _normalized_executable(candidate: Path) -> str | None:
    try:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return None
        return candidate.resolve().as_posix()
    except OSError:
        return None


def _is_codex_program(program: str) -> bool:
    return Path(program).name == _CODEX_PROGRAM_NAME


def _has_path_component(program: str) -> bool:
    return Path(program).name != program
