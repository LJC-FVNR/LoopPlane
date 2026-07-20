from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Collection, Iterable


DEFAULT_PRUNED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".cache",
        "__pycache__",
        ".pytest_cache",
        "artifacts",
        "cache",
        "caches",
        "checkpoints",
        "logs",
        "models",
        "node_modules",
        "outputs",
        "qualification_caches",
        "raw",
        "tmp",
        "wandb",
    }
)


@dataclass(frozen=True)
class FileDiscoveryResult:
    paths: tuple[Path, ...]
    scanned_entries: int
    scanned_directories: int
    truncated: bool
    limit_reason: str | None
    errors: tuple[str, ...]


def discover_files_bounded(
    roots: Iterable[Path],
    *,
    names: Collection[str] | None = None,
    prune_directory_names: Collection[str] = DEFAULT_PRUNED_DIRECTORY_NAMES,
    max_entries: int = 50_000,
    max_matches: int = 10_000,
    max_depth: int = 12,
    exclude_path: Callable[[Path], bool] | None = None,
) -> FileDiscoveryResult:
    """Discover files without ever materializing an unbounded directory tree.

    ``Path.rglob`` and ``glob("**/...``) must enumerate every descendant before
    callers can apply a limit.  This scanner consumes ``os.scandir`` lazily,
    prunes known artifact/cache directories, and stops once either budget is
    reached.  It is deliberately small and dependency-free so control-plane
    readers can use it without pulling artifact storage into their hot path.
    """

    wanted = {str(name) for name in names} if names is not None else None
    pruned = {str(name) for name in prune_directory_names}
    stack: list[tuple[Path, int]] = []
    matches: list[Path] = []
    errors: list[str] = []
    scanned_entries = 0
    scanned_directories = 0
    truncated = False
    limit_reason: str | None = None

    for root in roots:
        candidate = Path(root)
        try:
            excluded = exclude_path(candidate) if exclude_path is not None else False
        except (OSError, ValueError):
            excluded = False
        if excluded:
            continue
        if candidate.is_file():
            if wanted is None or candidate.name in wanted:
                matches.append(candidate)
            continue
        if candidate.is_dir() and all(existing[0] != candidate for existing in stack):
            stack.append((candidate, 0))

    while stack and not truncated:
        directory, depth = stack.pop()
        scanned_directories += 1
        try:
            iterator = os.scandir(directory)
        except OSError as error:
            errors.append(f"{directory}: {error}")
            continue
        with iterator:
            for entry in iterator:
                scanned_entries += 1
                if scanned_entries > max_entries:
                    truncated = True
                    limit_reason = "max_entries"
                    break
                try:
                    entry_path = Path(entry.path)
                    try:
                        excluded = exclude_path(entry_path) if exclude_path is not None else False
                    except (OSError, ValueError):
                        excluded = False
                    if excluded:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < max_depth and entry.name not in pruned:
                            stack.append((entry_path, depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if wanted is not None and entry.name not in wanted:
                    continue
                matches.append(entry_path)
                if len(matches) >= max_matches:
                    truncated = True
                    limit_reason = "max_matches"
                    break

    return FileDiscoveryResult(
        paths=tuple(sorted(set(matches), key=lambda path: path.as_posix())),
        scanned_entries=scanned_entries,
        scanned_directories=scanned_directories,
        truncated=truncated,
        limit_reason=limit_reason,
        errors=tuple(errors[:20]),
    )
