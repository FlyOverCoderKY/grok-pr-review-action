from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_action_yml_uses_xai_api_key_not_supergrok() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    install_script = (ROOT / "scripts" / "install-grok.sh").read_text(encoding="utf-8")
    run_script = (ROOT / "scripts" / "run-grok.sh").read_text(encoding="utf-8")
    cleanup_script = (ROOT / "scripts" / "cleanup-workdir.sh").read_text(encoding="utf-8")
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
    assert "validate-inputs" in text
    assert text.index("Validate inputs") < text.index("Require XAI_API_KEY")
    assert "github_timeout_seconds" in text
    assert "allow_unprofessional_tone" in text
    assert "prepare-workspace" in text
    assert "scripts/install-grok.sh" in text
    assert "scripts/run-grok.sh" in text
    assert "scripts/cleanup-workdir.sh" in text
    assert "--sandbox strict" in run_script
    assert "--no-subagents" in run_script
    assert "--disable-web-search" in run_script
    assert 'GROK_VERSION="1.0.5"' in install_script
    assert "sha256sum --check" in install_script
    assert 'rm -rf -- "$work"' in cleanup_script
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
    assert "Data sent to xAI" in text
    assert "requests and responses are retained for 30 days by default" in text
    assert "allow_unprofessional_tone" in text
    assert "Operational errors always fail" in text
