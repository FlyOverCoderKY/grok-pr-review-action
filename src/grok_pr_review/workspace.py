"""Materialize an inert source snapshot for the exact reviewed commit."""

from __future__ import annotations

import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

from grok_pr_review.scope import normalize_sha

MAX_SNAPSHOT_BYTES = 250 * 1024 * 1024
MAX_SNAPSHOT_FILES = 20_000

_CONTROL_DIRECTORIES = frozenset({".agents", ".claude", ".cursor", ".git", ".grok", ".windsurf"})
_CONTROL_FILES = frozenset(
    {
        ".cursorrules",
        ".mcp.json",
        ".windsurfrules",
        "agent.md",
        "agents.md",
        "claude.local.md",
        "claude.md",
        "grok.md",
    }
)


@dataclass(frozen=True)
class WorkspacePreparation:
    files_copied: int
    bytes_copied: int
    excluded_paths: tuple[str, ...]


class WorkspaceError(ValueError):
    """Raised when an exact, bounded review snapshot cannot be materialized."""


@dataclass(frozen=True)
class _TreeEntry:
    path: PurePosixPath
    object_id: str
    size: int
    mode: int


def prepare_review_workspace(
    source: Path,
    destination: Path,
    *,
    reviewed_sha: str,
) -> WorkspacePreparation:
    """Read regular files from reviewed_sha without using mutable checkout contents."""
    try:
        source_root = source.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(f"could not resolve source workspace: {exc}") from exc
    destination_root = destination.resolve(strict=False)
    commit = normalize_sha(reviewed_sha)
    if not source_root.is_dir():
        raise WorkspaceError("source workspace must be a directory")
    if commit is None or len(commit) != 40:
        raise WorkspaceError("reviewed_sha must be a full commit SHA")
    if destination_root == source_root or destination_root.is_relative_to(source_root):
        raise WorkspaceError("review workspace must be outside the source workspace")
    if destination_root.exists():
        raise WorkspaceError("review workspace destination already exists")
    git_executable = shutil.which("git")
    if git_executable is None:
        raise WorkspaceError("git executable was not found on PATH")

    entries, excluded = _list_tree(source_root, commit, git_executable)
    destination_root.mkdir(parents=True)
    try:
        process = subprocess.Popen(  # nosec B603
            [
                git_executable,
                "--no-replace-objects",
                "-C",
                str(source_root),
                "cat-file",
                "--batch",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        shutil.rmtree(destination_root)
        raise WorkspaceError(f"could not start git cat-file: {exc}") from exc
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        shutil.rmtree(destination_root)
        raise WorkspaceError("could not open git object streams")
    object_input = cast(BinaryIO, process.stdin)
    object_output = cast(BinaryIO, process.stdout)

    copied_files = 0
    copied_bytes = 0
    try:
        for entry in entries:
            object_input.write(f"{entry.object_id}\n".encode())
            object_input.flush()
            header = object_output.readline().decode("ascii", errors="replace").strip()
            expected_header = f"{entry.object_id} blob {entry.size}"
            if header != expected_header:
                raise WorkspaceError(
                    f"unexpected git object response for {entry.path.as_posix()!r}: {header!r}"
                )
            target_path = destination_root.joinpath(*entry.path.parts)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("wb") as output:
                copied = _copy_exact(object_output, output, entry.size)
            if copied != entry.size or object_output.read(1) != b"\n":
                raise WorkspaceError(f"git object was truncated: {entry.path.as_posix()!r}")
            target_path.chmod(entry.mode)
            copied_files += 1
            copied_bytes += entry.size
        object_input.close()
    except WorkspaceError:
        _stop_process(process)
        shutil.rmtree(destination_root)
        raise
    except OSError as exc:
        stderr = _stop_process(process)
        shutil.rmtree(destination_root)
        detail = stderr or str(exc)
        raise WorkspaceError(f"could not materialize reviewed commit: {detail}") from exc

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    return_code = process.wait()
    if return_code != 0:
        shutil.rmtree(destination_root)
        raise WorkspaceError(
            f"could not read reviewed commit {commit[:12]}: {stderr or 'git failed'}"
        )

    return WorkspacePreparation(
        files_copied=copied_files,
        bytes_copied=copied_bytes,
        excluded_paths=tuple(sorted(set(excluded))),
    )


def _list_tree(
    source: Path, commit: str, git_executable: str
) -> tuple[list[_TreeEntry], list[str]]:
    # Fixed argv is passed directly to Git and is never interpreted by a shell.
    try:
        completed = subprocess.run(  # nosec B603
            [
                git_executable,
                "--no-replace-objects",
                "-C",
                str(source),
                "ls-tree",
                "-r",
                "-z",
                "-l",
                "--full-tree",
                commit,
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError(f"could not inspect reviewed commit: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise WorkspaceError(
            f"could not inspect reviewed commit {commit[:12]}: {detail or 'git failed'}"
        )

    entries: list[_TreeEntry] = []
    excluded: list[str] = []
    total_bytes = 0
    for raw_entry in completed.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode_text, object_type, object_id, size_text = metadata.split()
            path_text = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkspaceError("reviewed commit contains an unsupported tree entry") from exc
        path = _safe_tree_path(path_text)
        if path is None:
            raise WorkspaceError(f"reviewed commit contains an unsafe path: {path_text!r}")
        if (
            _is_control_path(path)
            or object_type != b"blob"
            or mode_text not in {b"100644", b"100755"}
        ):
            excluded.append(path.as_posix())
            continue
        try:
            size = int(size_text)
            mode = int(mode_text, 8) & 0o777
            object_id_text = object_id.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkspaceError(f"invalid tree metadata for {path.as_posix()!r}") from exc
        if size < 0:
            raise WorkspaceError(f"invalid blob size for {path.as_posix()!r}")
        if len(entries) + 1 > MAX_SNAPSHOT_FILES:
            raise WorkspaceError(
                f"review snapshot exceeds the {MAX_SNAPSHOT_FILES}-file safety limit"
            )
        total_bytes += size
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise WorkspaceError(
                "review snapshot exceeds the "
                f"{MAX_SNAPSHOT_BYTES // (1024 * 1024)} MiB safety limit"
            )
        entries.append(_TreeEntry(path, object_id_text, size, mode))
    return entries, excluded


def _safe_tree_path(value: str) -> PurePosixPath | None:
    if not value or "\\" in value or any(ord(character) < 32 for character in value):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _is_control_path(path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.lower() for part in path.parts)
    return any(part in _CONTROL_DIRECTORIES for part in lowered_parts) or (
        bool(lowered_parts) and lowered_parts[-1] in _CONTROL_FILES
    )


def _stop_process(process: subprocess.Popen[bytes]) -> str:
    if process.poll() is None:
        process.kill()
    stderr = (
        process.stderr.read().decode("utf-8", errors="replace").strip() if process.stderr else ""
    )
    process.wait()
    return stderr


def _copy_exact(source: BinaryIO, destination: BinaryIO, size: int) -> int:
    copied = 0
    remaining = size
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            break
        destination.write(chunk)
        copied += len(chunk)
        remaining -= len(chunk)
    return copied
