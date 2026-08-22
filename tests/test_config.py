from __future__ import annotations

import pytest

from grok_pr_review.config import ActionConfig, ConfigError


def test_action_config_validates_and_normalizes_every_input() -> None:
    config = ActionConfig.from_env(
        {
            "PR_NUMBER": "17",
            "GITHUB_TOKEN": "test-token",
            "MODEL": " grok-test ",
            "EFFORT": "HIGH",
            "MAX_TURNS": "25",
            "FAIL_ON": "BUGS",
            "ROAST_LEVEL": "playful",
            "CUSTOM_INSTRUCTIONS": " Check invariants. ",
            "ALLOW_UNPROFESSIONAL_TONE": "false",
            "STATUS_COMMENTS": "yes",
            "MAX_DIFF_KB": "512",
            "REVIEW_SCOPE": "latest-commit",
            "GITHUB_TIMEOUT_SECONDS": "45",
        }
    )

    assert config.pr_number == 17
    assert config.model == "grok-test"
    assert config.effort == "high"
    assert config.fail_on == "bugs"
    assert config.custom_instructions == "Check invariants."
    assert config.status_comments is True
    assert config.review_scope == "latest-commit"
    assert config.github_timeout_seconds == 45


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PR_NUMBER", "0"),
        ("MODEL", "grok test"),
        ("MODEL", ".hidden"),
        ("MODEL", "g" * 201),
        ("EFFORT", "maximum"),
        ("MAX_TURNS", "0"),
        ("MAX_TURNS", "1001"),
        ("FAIL_ON", "sometimes"),
        ("STATUS_COMMENTS", "maybe"),
        ("MAX_DIFF_KB", "0"),
        ("REVIEW_SCOPE", "commit"),
        ("GITHUB_TIMEOUT_SECONDS", "601"),
    ],
)
def test_action_config_rejects_invalid_values(name: str, value: str) -> None:
    with pytest.raises(ConfigError):
        ActionConfig.from_env({"PR_NUMBER": "1", "GITHUB_TOKEN": "test-token", name: value})


@pytest.mark.parametrize("tone", ["savage", "diabolical"])
def test_unprofessional_public_tones_require_explicit_opt_in(tone: str) -> None:
    with pytest.raises(ConfigError, match="allow_unprofessional_tone"):
        ActionConfig.from_env({"PR_NUMBER": "1", "GITHUB_TOKEN": "test-token", "ROAST_LEVEL": tone})

    config = ActionConfig.from_env(
        {
            "PR_NUMBER": "1",
            "GITHUB_TOKEN": "test-token",
            "ROAST_LEVEL": tone,
            "ALLOW_UNPROFESSIONAL_TONE": "true",
        }
    )
    assert config.roast_level == tone


def test_custom_instructions_have_a_byte_budget() -> None:
    with pytest.raises(ConfigError, match="byte limit"):
        ActionConfig.from_env(
            {
                "PR_NUMBER": "1",
                "GITHUB_TOKEN": "test-token",
                "CUSTOM_INSTRUCTIONS": "🔍" * 4_001,
            }
        )


def test_required_pr_number_and_github_token_are_checked_up_front() -> None:
    with pytest.raises(ConfigError, match="pr_number"):
        ActionConfig.from_env({"GITHUB_TOKEN": "test-token"})
    with pytest.raises(ConfigError, match="github_token"):
        ActionConfig.from_env({"PR_NUMBER": "1"})
