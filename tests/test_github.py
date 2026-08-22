from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from grok_pr_review.github import STATUS_MARKER, GitHubCli
from grok_pr_review.result import MAX_GITHUB_BODY_BYTES, Issue, ReviewResult
from grok_pr_review.scope import GhError


class StubGitHub(GitHubCli):
    def __init__(self) -> None:
        super().__init__("owner/repo", env={})
        self.calls: list[tuple[list[str], str | None]] = []
        self.responses: list[str | Exception] = []

    def _api(self, args: list[str], stdin: str | None = None) -> str:
        self.calls.append((args, stdin))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_find_status_comment_flattens_pages_and_uses_latest_marker() -> None:
    github = StubGitHub()
    github.responses = [
        json.dumps(
            [
                [{"id": 1, "body": f"{STATUS_MARKER}\nold"}],
                [{"id": 2, "body": "ordinary"}, {"id": 3, "body": STATUS_MARKER}],
            ]
        )
    ]
    assert github.find_status_comment(7) == 3
    assert github.calls[0][0][:2] == ["--paginate", "--slurp"]


def test_upsert_status_creates_a_new_comment_when_marker_cannot_be_edited() -> None:
    github = StubGitHub()
    github.responses = [GhError("not your comment"), json.dumps({"id": 9})]
    assert github.upsert_status_comment(7, "running", 4) == 9
    assert "PATCH" in github.calls[0][0]
    assert "POST" in github.calls[1][0]


def test_compare_diff_requires_a_linear_ahead_range() -> None:
    github = StubGitHub()
    github.responses = [json.dumps({"status": "ahead", "behind_by": 0}), "diff body"]
    assert github.compare_diff("a" * 40, "b" * 40) == "diff body"
    assert len(github.calls) == 2

    diverged = StubGitHub()
    diverged.responses = [json.dumps({"status": "diverged", "behind_by": 2})]
    with pytest.raises(GhError, match="not a linear"):
        diverged.compare_diff("a" * 40, "b" * 40)
    assert len(diverged.calls) == 1


def test_post_review_falls_back_to_body_when_inline_comment_is_rejected() -> None:
    github = StubGitHub()
    github.responses = [GhError("invalid line"), json.dumps({"html_url": "https://review"})]
    result = ReviewResult(
        verdict="issues",
        summary="Found one issue.",
        issues=[Issue("bug", "src/app.py", 3, "Bug", "Broken behavior.")],
    )
    url = github.post_review(
        7,
        "a" * 40,
        result,
        scope="full-pr",
        model="grok-4.6",
        run_url="https://run",
    )
    assert url == "https://review"
    first_payload = json.loads(github.calls[0][1] or "{}")
    second_payload = json.loads(github.calls[1][1] or "{}")
    assert "comments" in first_payload
    assert "comments" not in second_payload


def test_exec_uses_an_argument_vector_and_converts_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    github = GitHubCli("owner/repo", env={"TOKEN": "value"})
    assert github.pr_diff(7) == "ok"
    assert captured["argv"] == ["gh", "pr", "diff", "7", "--repo", "owner/repo"]
    assert captured["check"] is False
    assert captured["timeout"] == 120
    assert "shell" not in captured

    def failed_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied")

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(GhError, match="denied"):
        github.pr_diff(7)


def test_exec_converts_timeouts_and_missing_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 17)

    monkeypatch.setattr(subprocess, "run", timed_out)
    github = GitHubCli("owner/repo", env={}, timeout_seconds=17)
    with pytest.raises(GhError, match="timed out after 17 seconds"):
        github.pr_diff(7)

    def missing(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(GhError, match="could not start"):
        github.pr_diff(7)


def test_post_review_continues_large_aggregate_output_without_losing_findings() -> None:
    github = StubGitHub()
    github.responses = [
        json.dumps({"html_url": "https://review"}),
        json.dumps({"html_url": "https://comment"}),
    ]
    result = ReviewResult(
        verdict="issues",
        summary="Large review.",
        issues=[Issue("risk", None, None, f"Finding {index}", "x" * 8_000) for index in range(8)],
    )

    assert (
        github.post_review(7, "a" * 40, result, scope="full-pr", model="grok-4.6", run_url="")
        == "https://review"
    )
    review_payload = json.loads(github.calls[0][1] or "{}")
    continuation_arg = next(value for value in github.calls[1][0] if value.startswith("body="))
    bodies = [review_payload["body"], continuation_arg.removeprefix("body=")]
    aggregate = "\n".join(bodies)
    assert all(len(body.encode("utf-8")) <= MAX_GITHUB_BODY_BYTES for body in bodies)
    for index in range(8):
        assert f"Finding {index}" in aggregate


def test_status_comment_body_is_bounded() -> None:
    github = StubGitHub()
    github.responses = [json.dumps({"id": 9})]

    assert github.upsert_status_comment(7, "🔍" * 20_000, None) == 9
    body_arg = next(value for value in github.calls[0][0] if value.startswith("body="))
    assert len(body_arg.removeprefix("body=").encode("utf-8")) <= MAX_GITHUB_BODY_BYTES
