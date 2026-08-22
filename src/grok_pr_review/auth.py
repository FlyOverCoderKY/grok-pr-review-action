"""xAI API-key auth helpers. The key itself is never written or printed."""

from __future__ import annotations

import os
from collections.abc import Sequence
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


def render_config_toml(model: str, additional_models: Sequence[str] = ()) -> str:
    """Return ~/.grok/config.toml contents that pin every model to api.x.ai."""
    try:
        chosen = parse_model(model)
        extra = [parse_model(name) for name in additional_models if name.strip()]
    except ConfigError as exc:
        raise AuthError(str(exc)) from exc
    text = f'[cli]\nauto_update = false\n\n[models]\ndefault = "{chosen}"\n'
    seen: set[str] = set()
    for name in [chosen, *extra]:
        if name in seen:
            continue
        seen.add(name)
        text += (
            "\n"
            f'[model."{name}"]\n'
            f'model = "{name}"\n'
            f'base_url = "{XAI_BASE_URL}"\n'
            'env_key = "XAI_API_KEY"\n'
        )
    return text


def write_grok_config(grok_home: Path, model: str, additional_models: Sequence[str] = ()) -> Path:
    """Write config.toml under GROK_HOME. Never persist the API key."""
    grok_home.mkdir(parents=True, exist_ok=True)
    path = grok_home / "config.toml"
    path.write_text(render_config_toml(model, additional_models), encoding="utf-8")
    path.chmod(0o600)
    return path
