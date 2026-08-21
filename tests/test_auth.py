from __future__ import annotations

from pathlib import Path

import pytest

from grok_pr_review.auth import (
    AuthError,
    render_config_toml,
    require_xai_api_key,
    write_grok_config,
)


def test_empty_api_key_fails_closed() -> None:
    with pytest.raises(AuthError, match="XAI_API_KEY is empty"):
        require_xai_api_key(env={})
    with pytest.raises(AuthError, match="XAI_API_KEY is empty"):
        require_xai_api_key(env={"XAI_API_KEY": "   "})


def test_present_api_key_is_accepted_without_echoing() -> None:
    require_xai_api_key(env={"XAI_API_KEY": "xai-not-a-real-key"})


def test_config_toml_points_model_at_api_x_ai() -> None:
    text = render_config_toml("grok-4.6")
    assert 'base_url = "https://api.x.ai/v1"' in text
    assert 'env_key = "XAI_API_KEY"' in text
    assert '[model."grok-4.6"]' in text
    assert "grok_auth_json" not in text
    assert "SuperGrok" not in text
    assert "xai-not-a-real-key" not in text


def test_config_file_never_contains_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-should-never-be-written")
    path = write_grok_config(tmp_path, "grok-4.6")
    written = path.read_text(encoding="utf-8")
    assert "xai-should-never-be-written" not in written
    assert 'env_key = "XAI_API_KEY"' in written


def test_invalid_model_rejected() -> None:
    with pytest.raises(AuthError):
        render_config_toml('grok"evil')
