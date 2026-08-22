from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from grok_pr_review.workspace import WorkspaceError, prepare_review_workspace


def _commit(source: Path, message: str = "snapshot") -> str:
    if not (source / ".git").exists():
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "config", "user.email", "test@example.test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "--all"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", message], check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_prepare_workspace_excludes_agent_controls_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "review"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (source / ".github").mkdir()
    (source / ".github" / "workflow.yml").write_text("name: test\n", encoding="utf-8")
    for directory in (".grok", ".claude", ".agents", ".cursor", ".windsurf"):
        path = source / directory
        path.mkdir()
        (path / "control.txt").write_text("execute me\n", encoding="utf-8")
    for filename in (
        "AGENTS.md",
        "CLAUDE.md",
        "GROK.md",
        ".mcp.json",
        ".cursorrules",
        ".windsurfrules",
    ):
        (source / filename).write_text("execute me\n", encoding="utf-8")
    reviewed_sha = _commit(source)

    result = prepare_review_workspace(source, destination, reviewed_sha=reviewed_sha)

    assert result.files_copied == 2
    assert (destination / "src" / "app.py").is_file()
    assert (destination / ".github" / "workflow.yml").is_file()
    assert not (destination / ".grok").exists()
    assert not (destination / ".windsurf").exists()
    assert not (destination / "AGENTS.md").exists()
    assert not (destination / "GROK.md").exists()
    assert not (destination / ".cursorrules").exists()
    assert ".mcp.json" in result.excluded_paths
    assert "GROK.md" in result.excluded_paths


def test_prepare_workspace_uses_exact_commit_not_mutable_checkout(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tracked = source / "app.py"
    tracked.write_text("value = 1\n", encoding="utf-8")
    (source / ".gitignore").write_text("build/\n", encoding="utf-8")
    (source / ".gitattributes").write_text(
        "hidden.py export-ignore\nmetadata.txt export-subst\n", encoding="utf-8"
    )
    (source / "hidden.py").write_text("tracked = True\n", encoding="utf-8")
    (source / "metadata.txt").write_text("$Format:%H$\n", encoding="utf-8")
    lfs_pointer = (
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + ("a" * 64) + "\nsize 123\n"
    )
    (source / "asset.bin").write_text(lfs_pointer, encoding="utf-8")
    reviewed_sha = _commit(source, "reviewed")
    tracked.write_text("value = 2\n", encoding="utf-8")
    (source / "untracked-secret.txt").write_text("secret\n", encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "generated.bin").write_bytes(b"generated")
    subprocess.run(["git", "-C", str(source), "add", "app.py"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "commit", "-m", "replacement"],
        check=True,
        capture_output=True,
    )
    replacement_sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(source), "replace", reviewed_sha, replacement_sha], check=True)

    destination = tmp_path / "review"
    prepare_review_workspace(source, destination, reviewed_sha=reviewed_sha)

    assert (destination / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (destination / ".gitignore").is_file()
    assert (destination / "hidden.py").is_file()
    assert (destination / "metadata.txt").read_text(encoding="utf-8") == "$Format:%H$\n"
    assert (destination / "asset.bin").read_text(encoding="utf-8") == lfs_pointer
    assert not (destination / "untracked-secret.txt").exists()
    assert not (destination / "build").exists()


def test_prepare_workspace_excludes_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = source / "escape.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    reviewed_sha = _commit(source)

    destination = tmp_path / "review"
    result = prepare_review_workspace(source, destination, reviewed_sha=reviewed_sha)

    assert "escape.txt" in result.excluded_paths
    assert not (destination / "escape.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows does not apply POSIX executable bits")
def test_prepare_workspace_preserves_executable_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    script = source / "tool.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    script.chmod(0o755)
    reviewed_sha = _commit(source)

    destination = tmp_path / "review"
    prepare_review_workspace(source, destination, reviewed_sha=reviewed_sha)

    assert (destination / "tool.sh").stat().st_mode & stat.S_IXUSR


def test_prepare_workspace_rejects_unsafe_destinations_and_unknown_commits(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
    reviewed_sha = _commit(source)
    with pytest.raises(WorkspaceError, match="outside"):
        prepare_review_workspace(source, source / "nested", reviewed_sha=reviewed_sha)

    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(WorkspaceError, match="already exists"):
        prepare_review_workspace(source, destination, reviewed_sha=reviewed_sha)

    missing = tmp_path / "missing-commit"
    with pytest.raises(WorkspaceError, match="inspect|materialize|archive"):
        prepare_review_workspace(source, missing, reviewed_sha="f" * 40)
    assert not missing.exists()

    with pytest.raises(WorkspaceError, match="resolve source"):
        prepare_review_workspace(
            tmp_path / "not-a-checkout",
            tmp_path / "missing-source",
            reviewed_sha=reviewed_sha,
        )
