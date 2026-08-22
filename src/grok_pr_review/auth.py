"""xAI API-key auth helpers. The key itself is never written or printed."""

from __future__ import annotations

import os
from pathlib import Path

from grok_pr_review.config import ConfigError, parse_model

XAI_BASE_URL = "https://api.x.ai/v1"


class AuthError(ValueError):
    """Raised when auth configuration is missing or invalid."""


def require_xai_api_key(env: dict[str, str] | None = None) -> None:
    """Fail closed when XAI_API_KEY is unset or whitespace-only."""
    source = os.environ if env is None else env
    key = source.get("XAI_API_KEY", "")
    if key.strip() == "":
        raise AuthError(
            "XAI_API_KEY is empty. Set a repository secret and pass it as the "
            "XAI_API_KEY environment variable. This action authenticates against "
            "https://api.x.ai/v1 only and does not use grok login, SuperGrok, "
            "or grok_auth_json."
        )


def render_config_toml(model: str) -> str:
    """Return ~/.grok/config.toml contents that pin the model to api.x.ai."""
    try:
        chosen = parse_model(model)
    except ConfigError as exc:
        raise AuthError(str(exc)) from exc
    return (
        "[cli]\n"
        "auto_update = false\n"
        "\n"
        "[models]\n"
        f'default = "{chosen}"\n'
        "\n"
        f'[model."{chosen}"]\n'
        f'model = "{chosen}"\n'
        f'base_url = "{XAI_BASE_URL}"\n'
        'env_key = "XAI_API_KEY"\n'
    )


def write_grok_config(grok_home: Path, model: str) -> Path:
    """Write config.toml under GROK_HOME. Never persist the API key."""
    grok_home.mkdir(parents=True, exist_ok=True)
    path = grok_home / "config.toml"
    path.write_text(render_config_toml(model), encoding="utf-8")
    path.chmod(0o600)
    return path
