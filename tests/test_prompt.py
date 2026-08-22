from __future__ import annotations

from pathlib import Path

import pytest

from grok_pr_review.config import ConfigError
from grok_pr_review.prompt import PromptContext, build_prompt
from grok_pr_review.scope import DiffPlan, DiffRequest, plan_diff, truncate_diff

FIXTURES = Path(__file__).parent / "fixtures"
FULL_PR_DIFF = (FIXTURES / "full_pr.diff").read_text(encoding="utf-8")
LATEST_DIFF = (FIXTURES / "latest_commit.diff").read_text(encoding="utf-8")
BEFORE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
AFTER = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

PR = {
    "number": 7,
    "title": "Add follow-up",
    "body": "Please review the new work only.",
    "url": "https://example.test/pr/7",
    "author": {"login": "nathan"},
    "headRefOid": AFTER,
    "baseRefName": "main",
    "headRefName": "feature",
    "additions": 40,
    "deletions": 2,
    "changedFiles": 3,
}


def _prompt(plan: DiffPlan, diff: str, max_diff_kb: int = 300) -> str:
    return build_prompt(
        PromptContext(
            pr=PR,
            plan=plan,
            truncation=truncate_diff(diff, max_diff_kb),
            roast_level="professional",
            custom_instructions="Focus on regressions.",
        )
    )


def test_latest_commit_prompt_does_not_embed_full_pr_diff() -> None:
    plan = plan_diff(
        DiffRequest(
            scope="latest-commit",
            before_sha=BEFORE,
            after_sha=AFTER,
            head_sha=AFTER,
        )
    )
    prompt = _prompt(plan, LATEST_DIFF)
    assert "UNIQUE_LATEST_COMMIT_HUNK" in prompt
    assert "UNIQUE_FULL_PR_HUNK" not in prompt
    assert "FULL_PR_ONLY_FILE" not in prompt
    assert "latest-commit" in prompt
    assert "full pull request diff was not fetched" in prompt
    assert f"{BEFORE[:12]}...{AFTER[:12]}" in prompt
    assert "Focus on regressions." in prompt


def test_full_pr_prompt_embeds_full_fixture() -> None:
    plan = plan_diff(
        DiffRequest(scope="full-pr", before_sha=BEFORE, after_sha=AFTER, head_sha=AFTER)
    )
    prompt = _prompt(plan, FULL_PR_DIFF)
    assert "UNIQUE_FULL_PR_HUNK" in prompt
    assert "full-pr" in prompt
    assert "gh pr diff" in prompt


def test_missing_before_prompt_states_the_fallback() -> None:
    plan = plan_diff(
        DiffRequest(scope="latest-commit", before_sha=None, after_sha=None, head_sha=AFTER)
    )
    prompt = _prompt(plan, LATEST_DIFF)
    assert "NOTICE:" in prompt
    assert "single latest commit" in prompt
    assert "UNIQUE_LATEST_COMMIT_HUNK" in prompt
    assert "UNIQUE_FULL_PR_HUNK" not in prompt
    assert "full pull request diff was not fetched" in prompt


def test_truncation_notice_appears_in_prompt() -> None:
    huge = "UNIQUE_FULL_PR_HUNK\n" + ("y" * 5000)
    plan = plan_diff(DiffRequest(scope="full-pr", before_sha=None, after_sha=AFTER, head_sha=AFTER))
    prompt = _prompt(plan, huge, max_diff_kb=1)
    assert "NOTICE:" in prompt
    assert "truncated" in prompt.lower()
    assert "max_diff_kb=1" in prompt
    assert len(prompt.encode("utf-8")) < len(huge.encode("utf-8")) + 8000


def test_prompt_rejects_unknown_or_unapproved_public_comment_tones() -> None:
    plan = plan_diff(
        DiffRequest(scope="full-pr", before_sha=BEFORE, after_sha=AFTER, head_sha=AFTER)
    )
    base = {
        "pr": PR,
        "plan": plan,
        "truncation": truncate_diff(FULL_PR_DIFF, 300),
        "custom_instructions": "",
    }

    with pytest.raises(ConfigError):
        build_prompt(PromptContext(roast_level="unknown", **base))
    with pytest.raises(ConfigError, match="allow_unprofessional_tone"):
        build_prompt(PromptContext(roast_level="savage", **base))

    prompt = build_prompt(
        PromptContext(roast_level="savage", allow_unprofessional_tone=True, **base)
    )
    assert "blunt and unsparing" in prompt
