from __future__ import annotations

from pathlib import Path

import pytest

from grok_pr_review.scope import (
    COMPARE_FAILED_NOTICE,
    MISSING_BEFORE_NOTICE,
    DiffRequest,
    GhError,
    collect_review_material,
    plan_diff,
    truncate_diff,
)

FIXTURES = Path(__file__).parent / "fixtures"
FULL_PR_DIFF = (FIXTURES / "full_pr.diff").read_text(encoding="utf-8")
LATEST_DIFF = (FIXTURES / "latest_commit.diff").read_text(encoding="utf-8")
BEFORE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
AFTER = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def pr_view(self, number: int) -> dict[str, object]:
        self.calls.append(("pr_view", number))
        return {
            "number": number,
            "title": "Demo",
            "body": "A learning PR",
            "url": "https://example.test/pr/7",
            "author": {"login": "nathan"},
            "headRefOid": AFTER,
            "baseRefName": "main",
            "headRefName": "feature",
            "additions": 40,
            "deletions": 2,
            "changedFiles": 3,
        }

    def pr_diff(self, number: int) -> str:
        self.calls.append(("pr_diff", number))
        return FULL_PR_DIFF

    def compare_diff(self, before: str, after: str) -> str:
        self.calls.append(("compare_diff", before, after))
        return LATEST_DIFF

    def commit_diff(self, sha: str) -> str:
        self.calls.append(("commit_diff", sha))
        return LATEST_DIFF


class CompareFails(FakeGitHub):
    def compare_diff(self, before: str, after: str) -> str:
        self.calls.append(("compare_diff", before, after))
        raise GhError("Not Found")


def _names(fake: FakeGitHub) -> list[str]:
    return [str(call[0]) for call in fake.calls]


def test_full_pr_plan_uses_gh_pr_diff() -> None:
    plan = plan_diff(
        DiffRequest(scope="full-pr", before_sha=BEFORE, after_sha=AFTER, head_sha=AFTER)
    )
    assert plan.kind == "full-pr"
    assert plan.fallback_notice is None


def test_latest_commit_prefers_before_after_range() -> None:
    plan = plan_diff(
        DiffRequest(
            scope="latest-commit",
            before_sha=BEFORE,
            after_sha=AFTER,
            head_sha=AFTER,
        )
    )
    assert plan.kind == "commit-range"
    assert plan.from_sha == BEFORE
    assert plan.to_sha == AFTER
    assert plan.fallback_notice is None
    assert plan.kind != "full-pr"


def test_missing_before_falls_back_to_single_latest_commit() -> None:
    plan = plan_diff(
        DiffRequest(scope="latest-commit", before_sha=None, after_sha=None, head_sha=AFTER)
    )
    assert plan.kind == "single-commit"
    assert plan.to_sha == AFTER
    assert plan.fallback_notice == MISSING_BEFORE_NOTICE
    assert plan.kind != "full-pr"


def test_zero_before_sha_is_treated_as_missing() -> None:
    plan = plan_diff(
        DiffRequest(
            scope="latest-commit",
            before_sha="0000000000000000000000000000000000000000",
            after_sha=AFTER,
            head_sha=AFTER,
        )
    )
    assert plan.kind == "single-commit"
    assert plan.fallback_notice == MISSING_BEFORE_NOTICE


def test_latest_commit_without_any_head_refuses_full_pr() -> None:
    with pytest.raises(ValueError, match="Refusing to fall back to the full PR diff"):
        plan_diff(
            DiffRequest(scope="latest-commit", before_sha=None, after_sha=None, head_sha=None)
        )


def test_collect_latest_commit_does_not_fetch_full_pr_diff() -> None:
    github = FakeGitHub()
    collected = collect_review_material(
        pr_number=7,
        request=DiffRequest(
            scope="latest-commit",
            before_sha=BEFORE,
            after_sha=AFTER,
            head_sha=AFTER,
        ),
        max_diff_kb=300,
        github=github,
    )
    assert "pr_diff" not in _names(github)
    assert "compare_diff" in _names(github)
    assert "UNIQUE_LATEST_COMMIT_HUNK" in collected.diff
    assert "UNIQUE_FULL_PR_HUNK" not in collected.diff
    assert "FULL_PR_ONLY_FILE" not in collected.diff


def test_collect_missing_before_uses_commit_api_not_pr_diff() -> None:
    github = FakeGitHub()
    collected = collect_review_material(
        pr_number=7,
        request=DiffRequest(
            scope="latest-commit",
            before_sha=None,
            after_sha=None,
            head_sha=None,  # filled from pr_view headRefOid
        ),
        max_diff_kb=300,
        github=github,
    )
    assert collected.plan.kind == "single-commit"
    assert collected.plan.fallback_notice == MISSING_BEFORE_NOTICE
    assert "pr_diff" not in _names(github)
    assert "commit_diff" in _names(github)
    assert "UNIQUE_FULL_PR_HUNK" not in collected.diff


def test_compare_failure_falls_back_to_single_commit_not_full_pr() -> None:
    github = CompareFails()
    collected = collect_review_material(
        pr_number=7,
        request=DiffRequest(
            scope="latest-commit",
            before_sha=BEFORE,
            after_sha=AFTER,
            head_sha=AFTER,
        ),
        max_diff_kb=300,
        github=github,
    )
    assert collected.plan.kind == "single-commit"
    assert collected.plan.fallback_notice == COMPARE_FAILED_NOTICE
    assert "pr_diff" not in _names(github)
    assert "UNIQUE_FULL_PR_HUNK" not in collected.diff


def test_collect_full_pr_embeds_the_full_fixture() -> None:
    github = FakeGitHub()
    collected = collect_review_material(
        pr_number=7,
        request=DiffRequest(scope="full-pr", before_sha=None, after_sha=None, head_sha=AFTER),
        max_diff_kb=300,
        github=github,
    )
    assert "pr_diff" in _names(github)
    assert "UNIQUE_FULL_PR_HUNK" in collected.diff


def test_truncate_diff_adds_notice_and_caps_size() -> None:
    blob = "UNIQUE_FULL_PR_HUNK\n" + ("x" * 8000)
    result = truncate_diff(blob, max_diff_kb=1)
    assert result.truncated is True
    assert result.embedded_bytes <= 1024
    assert result.original_bytes > 1024
    assert result.notice is not None
    assert "truncated" in result.notice.lower()
    assert "max_diff_kb=1" in result.notice


def test_truncate_diff_keeps_small_payloads() -> None:
    result = truncate_diff(LATEST_DIFF, max_diff_kb=300)
    assert result.truncated is False
    assert result.notice is None
    assert result.text == LATEST_DIFF


def test_truncate_diff_stops_at_a_file_boundary() -> None:
    first = "diff --git a/first.py b/first.py\n" + ("+first\n" * 100)
    second = "diff --git a/second.py b/second.py\n" + ("+second\n" * 100)
    result = truncate_diff(first + second, max_diff_kb=1)
    assert result.truncated is True
    assert "first.py" in result.text
    assert "second.py" not in result.text
    assert result.text == first


def test_truncate_diff_never_discards_most_of_the_budget() -> None:
    huge_single_hunk = "diff --git a/big.py b/big.py\n@@ -1,1 +1,3000 @@\n" + ("+padding\n" * 3000)
    result = truncate_diff(huge_single_hunk, max_diff_kb=1)
    assert result.truncated is True
    assert result.embedded_bytes <= 1024
    assert result.embedded_bytes > 512
    assert "big.py" in result.text


def test_full_pr_fails_with_accurate_error_when_head_is_missing() -> None:
    class MissingHead(FakeGitHub):
        def pr_view(self, number: int) -> dict[str, object]:
            self.calls.append(("pr_view", number))
            return {"number": number}

    with pytest.raises(GhError, match="head SHA is missing"):
        collect_review_material(
            pr_number=7,
            request=DiffRequest(scope="full-pr", before_sha=None, after_sha=None, head_sha=AFTER),
            max_diff_kb=300,
            github=MissingHead(),
        )

    class HeadVanishes(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.views = 0

        def pr_view(self, number: int) -> dict[str, object]:
            self.views += 1
            self.calls.append(("pr_view", number))
            if self.views == 1:
                return {"number": number, "headRefOid": AFTER}
            return {"number": number}

    with pytest.raises(GhError, match="head SHA is missing"):
        collect_review_material(
            pr_number=7,
            request=DiffRequest(scope="full-pr", before_sha=None, after_sha=None, head_sha=AFTER),
            max_diff_kb=300,
            github=HeadVanishes(),
        )


def test_full_pr_fails_if_head_changes_during_collection() -> None:
    class MovingHead(FakeGitHub):
        def __init__(self) -> None:
            super().__init__()
            self.views = 0

        def pr_view(self, number: int) -> dict[str, object]:
            self.views += 1
            sha = AFTER if self.views == 1 else "c" * 40
            self.calls.append(("pr_view", (str(number),)))
            return {"number": number, "headRefOid": sha}

    with pytest.raises(GhError, match="PR head changed"):
        collect_review_material(
            pr_number=7,
            request=DiffRequest(scope="full-pr", before_sha=None, after_sha=None, head_sha=AFTER),
            max_diff_kb=300,
            github=MovingHead(),
        )
