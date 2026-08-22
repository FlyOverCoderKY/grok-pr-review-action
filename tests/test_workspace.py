from __future__ import annotations

import os
from pathlib import Path

import pytest

from grok_pr_review.workspace import prepare_review_workspace


def test_prepare_workspace_excludes_agent_controls_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "review"
    (source / "src").mkdir(parents=True)
    (source / "src" / "app.py").write_text("print('safe')\n", encoding="utf-8")
    (source / ".github").mkdir()
    (source / ".github" / "workflow.yml").write_text("name: test\n", encoding="utf-8")
    for directory in (".git", ".grok", ".claude", ".agents", ".cursor"):
        path = source / directory
        path.mkdir()
        (path / "control.txt").write_text("execute me\n", encoding="utf-8")
    for filename in ("AGENTS.md", "CLAUDE.md", ".mcp.json"):
        (source / filename).write_text("execute me\n", encoding="utf-8")

    result = prepare_review_workspace(source, destination)

    assert result.files_copied == 2
    assert (destination / "src" / "app.py").is_file()
    assert (destination / ".github" / "workflow.yml").is_file()
    assert not (destination / ".grok").exists()
    assert not (destination / "AGENTS.md").exists()
    assert ".mcp.json" in result.excluded_paths


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

    destination = tmp_path / "review"
    result = prepare_review_workspace(source, destination)

    assert "escape.txt" in result.excluded_paths
    assert not (destination / "escape.txt").exists()


def test_prepare_workspace_rejects_unsafe_destinations(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="outside"):
        prepare_review_workspace(source, source / "nested")

    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        prepare_review_workspace(source, destination)
