from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_action_yml_uses_xai_api_key_not_supergrok() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "review_scope" in text
    assert "latest-commit" in text
    assert "full-pr" in text
    assert "XAI_API_KEY" in text
    assert "check-auth" in text
    assert "write-config" in text
    assert "grok_auth_json" not in text
    assert "GROK_AUTH_JSON" not in text
    assert "SuperGrok" not in text
    assert "grok login" not in text
    assert "always()" in text or "if: ${{ always() }}" in text
    assert "--sandbox strict" in text
    assert "--no-subagents" in text
    assert "prepare-workspace" in text
    assert "grok-1.0.5" not in text  # URL is assembled from the separately pinned version.
    assert 'GROK_VERSION="1.0.5"' in text
    assert "sha256sum --check" in text
    assert 'rm -f "$HOME/.grok/' not in text
    assert 'rm -rf "$HOME/.grok' not in text


def test_readme_explains_latest_commit_and_auth() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "latest-commit" in text
    assert "full-pr" in text
    assert "XAI_API_KEY" in text
    assert "synchronize" in text
    assert "RetireGolden" in text
    assert "SuperGrok" in text  # mentioned as something we do not require
    assert "grok_auth_json" not in text.lower()
