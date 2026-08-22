"""Typed validation for composite-action inputs and environment configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, cast

from grok_pr_review.scope import ScopeName, parse_scope

Effort = Literal["", "low", "medium", "high", "xhigh"]
FailOn = Literal["never", "bugs", "any"]
RoastLevel = Literal["professional", "playful", "savage", "diabolical"]

DEFAULT_MODEL = "grok-4.6"
DEFAULT_GITHUB_TIMEOUT_SECONDS = 120
MAX_CUSTOM_INSTRUCTIONS_BYTES = 16_000
MAX_DIFF_KB = 10_240
MAX_GITHUB_TIMEOUT_SECONDS = 600
MAX_TURNS = 1_000
ROAST_LEVELS = ("professional", "playful", "savage", "diabolical")
UNPROFESSIONAL_ROAST_LEVELS = {"savage", "diabolical"}

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Raised when an action input is invalid or internally inconsistent."""


@dataclass(frozen=True)
class ActionConfig:
    pr_number: int
    model: str
    effort: Effort
    max_turns: int
    fail_on: FailOn
    roast_level: RoastLevel
    custom_instructions: str
    allow_unprofessional_tone: bool
    status_comments: bool
    max_diff_kb: int
    review_scope: ScopeName
    github_timeout_seconds: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ActionConfig:
        parse_required_secret(env.get("GITHUB_TOKEN", ""), "github_token")
        allow_unprofessional_tone = parse_bool(
            env.get("ALLOW_UNPROFESSIONAL_TONE", "false"),
            "allow_unprofessional_tone",
        )
        return cls(
            pr_number=parse_bounded_int(
                env.get("PR_NUMBER", ""),
                "pr_number",
                minimum=1,
                maximum=2_147_483_647,
            ),
            model=parse_model(env.get("MODEL", DEFAULT_MODEL)),
            effort=parse_effort(env.get("EFFORT", "")),
            max_turns=parse_bounded_int(
                env.get("MAX_TURNS", "50"), "max_turns", minimum=1, maximum=MAX_TURNS
            ),
            fail_on=parse_fail_on(env.get("FAIL_ON", "never")),
            roast_level=parse_roast_level(
                env.get("ROAST_LEVEL", "professional"),
                allow_unprofessional=allow_unprofessional_tone,
            ),
            custom_instructions=parse_custom_instructions(env.get("CUSTOM_INSTRUCTIONS", "")),
            allow_unprofessional_tone=allow_unprofessional_tone,
            status_comments=parse_bool(env.get("STATUS_COMMENTS", "true"), "status_comments"),
            max_diff_kb=parse_bounded_int(
                env.get("MAX_DIFF_KB", "300"),
                "max_diff_kb",
                minimum=1,
                maximum=MAX_DIFF_KB,
            ),
            review_scope=parse_review_scope(env.get("REVIEW_SCOPE", "full-pr")),
            github_timeout_seconds=parse_bounded_int(
                env.get("GITHUB_TIMEOUT_SECONDS", str(DEFAULT_GITHUB_TIMEOUT_SECONDS)),
                "github_timeout_seconds",
                minimum=1,
                maximum=MAX_GITHUB_TIMEOUT_SECONDS,
            ),
        )


def parse_model(value: str) -> str:
    chosen = value.strip()
    if not chosen or not _MODEL_RE.fullmatch(chosen):
        raise ConfigError(
            "model must be a 1-200 character identifier starting with an ASCII letter or digit"
        )
    return chosen


def parse_effort(value: str) -> Effort:
    chosen = value.strip().lower()
    if chosen not in {"", "low", "medium", "high", "xhigh"}:
        raise ConfigError("effort must be empty, low, medium, high, or xhigh")
    return cast(Effort, chosen)


def parse_fail_on(value: str) -> FailOn:
    chosen = value.strip().lower() or "never"
    if chosen not in {"never", "bugs", "any"}:
        raise ConfigError("fail_on must be never, bugs, or any")
    return cast(FailOn, chosen)


def parse_roast_level(value: str, *, allow_unprofessional: bool = False) -> RoastLevel:
    chosen = value.strip().lower() or "professional"
    if chosen not in ROAST_LEVELS:
        raise ConfigError("roast_level must be professional, playful, savage, or diabolical")
    if chosen in UNPROFESSIONAL_ROAST_LEVELS and not allow_unprofessional:
        raise ConfigError(
            "savage and diabolical public-comment tones require allow_unprofessional_tone=true"
        )
    return cast(RoastLevel, chosen)


def parse_custom_instructions(value: str) -> str:
    chosen = value.strip()
    size = len(chosen.encode("utf-8"))
    if size > MAX_CUSTOM_INSTRUCTIONS_BYTES:
        raise ConfigError(
            f"custom_instructions exceeds the {MAX_CUSTOM_INSTRUCTIONS_BYTES}-byte limit"
        )
    return chosen


def parse_required_secret(value: str, name: str) -> str:
    chosen = value.strip()
    if not chosen:
        raise ConfigError(f"{name} must not be empty")
    return chosen


def parse_review_scope(value: str) -> ScopeName:
    try:
        return parse_scope(value)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def parse_bool(value: str, name: str) -> bool:
    chosen = value.strip().lower()
    if chosen in _TRUE_VALUES:
        return True
    if chosen in _FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be true or false")


def parse_bounded_int(value: str, name: str, *, minimum: int, maximum: int) -> int:
    try:
        chosen = int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if chosen < minimum or chosen > maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return chosen
