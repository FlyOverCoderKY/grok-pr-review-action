"""Create an inert source snapshot for the Grok review process."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_CONTROL_DIRECTORIES = frozenset({".agents", ".claude", ".cursor", ".git", ".grok"})
_CONTROL_FILES = frozenset(
    {
        ".mcp.json",
        "agent.md",
        "agents.md",
        "claude.local.md",
        "claude.md",
    }
)


@dataclass(frozen=True)
class WorkspacePreparation:
    files_copied: int
    excluded_paths: tuple[str, ...]


def prepare_review_workspace(source: Path, destination: Path) -> WorkspacePreparation:
    """Copy source into a new directory without executable agent configuration.

    Symlinks are excluded instead of followed. This prevents a contributor-controlled
    link from exposing files outside the review snapshot to read-only tools.
    """
    source_root = source.resolve(strict=True)
    destination_root = destination.resolve(strict=False)
    if not source_root.is_dir():
        raise ValueError("source workspace must be a directory")
    if destination_root == source_root or destination_root.is_relative_to(source_root):
        raise ValueError("review workspace must be outside the source workspace")
    if destination_root.exists():
        raise ValueError("review workspace destination already exists")

    destination_root.mkdir(parents=True)
    excluded: list[str] = []
    copied = 0

    for root, directory_names, file_names in os.walk(source_root, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(source_root)
        target_root = destination_root / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        retained_directories: list[str] = []
        for name in directory_names:
            source_path = root_path / name
            relative_path = relative_root / name
            if _is_control_path(relative_path) or source_path.is_symlink():
                excluded.append(relative_path.as_posix())
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            source_path = root_path / name
            relative_path = relative_root / name
            if _is_control_path(relative_path) or source_path.is_symlink():
                excluded.append(relative_path.as_posix())
                continue
            if not source_path.is_file():
                excluded.append(relative_path.as_posix())
                continue
            target_path = destination_root / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path, follow_symlinks=False)
            copied += 1

    return WorkspacePreparation(files_copied=copied, excluded_paths=tuple(sorted(excluded)))


def _is_control_path(path: Path) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    return any(part in _CONTROL_DIRECTORIES for part in lowered_parts) or (
        bool(lowered_parts) and lowered_parts[-1] in _CONTROL_FILES
    )
