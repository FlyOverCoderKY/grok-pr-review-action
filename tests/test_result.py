from __future__ import annotations

import json

from grok_pr_review.result import (
    MAX_REVIEW_BODY_BYTES,
    Issue,
    ReviewResult,
    format_incomplete_comment,
    format_review_body,
    mark_partial,
    parse_grok_output,
    should_fail_job,
)

FINDINGS = {
    "summary": "One real bug in the follow-up.",
    "issues": [
        {
            "severity": "bug",
            "path": "followup.py",
            "line": 3,
            "title": "Off-by-one",
            "detail": "Loop walks one past the buffer.",
        }
    ],
}


def test_parse_fenced_json_inside_grok_envelope() -> None:
    envelope = {
        "text": "Thinking...\n\n```json\n" + json.dumps(FINDINGS) + "\n```\n",
        "stopReason": "end_turn",
    }
    result = parse_grok_output(json.dumps(envelope))
    assert result.verdict == "issues"
    assert result.issue_count == 1
    assert result.bug_count == 1
    assert result.issues[0].path == "followup.py"
    assert result.incomplete is False


def test_parse_clean_findings() -> None:
    envelope = {
        "text": json.dumps({"summary": "Looks good.", "issues": []}),
        "stopReason": "end_turn",
    }
    result = parse_grok_output(json.dumps(envelope))
    assert result.verdict == "clean"
    assert result.issue_count == 0


def test_missing_json_is_error() -> None:
    envelope = {"text": "I started reviewing and then wandered off.", "stopReason": "end_turn"}
    result = parse_grok_output(json.dumps(envelope), exit_code=0)
    assert result.verdict == "error"
    assert result.incomplete_reason is not None
    assert "no structured JSON" in result.incomplete_reason


def test_malformed_findings_fail_closed() -> None:
    payloads = [
        {"summary": "Looks good"},
        {"summary": "Looks good", "issues": "none"},
        {"summary": "Looks good", "issues": [None, "bad"]},
        {
            "summary": "Looks good",
            "issues": [
                {
                    "severity": "bug",
                    "path": "../outside.py",
                    "line": 1,
                    "title": "Escape",
                    "detail": "Unsafe path.",
                }
            ],
        },
        {
            "summary": "Looks good",
            "issues": [
                {
                    "severity": "risk",
                    "path": "file.py\n@maintainers",
                    "line": 1,
                    "title": "Injected location",
                    "detail": "Unsafe path formatting.",
                }
            ],
        },
    ]
    for payload in payloads:
        envelope = {"text": json.dumps(payload), "stopReason": "EndTurn"}
        result = parse_grok_output(json.dumps(envelope))
        assert result.verdict == "error"
        assert result.incomplete_reason is not None
        assert "invalid structured findings" in result.incomplete_reason


def test_nonzero_exit_with_valid_findings_fails_closed() -> None:
    envelope = {
        "text": json.dumps({"summary": "Looks good.", "issues": []}),
        "stopReason": "EndTurn",
    }
    result = parse_grok_output(json.dumps(envelope), exit_code=1)
    assert result.verdict == "error"
    assert result.incomplete_reason == "Grok exited unsuccessfully with code 1."


def test_missing_or_non_success_stop_reason_fails_closed() -> None:
    text = json.dumps({"summary": "Looks good.", "issues": []})
    missing = parse_grok_output(json.dumps({"text": text}))
    cancelled = parse_grok_output(json.dumps({"text": text, "stopReason": "Cancelled"}))
    assert missing.verdict == "error"
    assert cancelled.verdict == "error"


def test_max_turns_is_incomplete_error() -> None:
    envelope = {
        "text": "Still reading files.",
        "stopReason": "max_turns",
    }
    result = parse_grok_output(json.dumps(envelope), exit_code=1)
    assert result.verdict == "error"
    assert result.incomplete_reason is not None
    assert "max_turns" in result.incomplete_reason


def test_fail_on_never_does_not_fail_for_issues_or_errors() -> None:
    issues = parse_grok_output(json.dumps({"text": json.dumps(FINDINGS), "stopReason": "end_turn"}))
    error = ReviewResult(verdict="error", summary="", incomplete_reason="no JSON")
    assert should_fail_job("never", issues) is False
    assert should_fail_job("never", error) is False
    assert should_fail_job("bugs", issues) is True
    assert should_fail_job("bugs", error) is False
    assert should_fail_job("any", issues) is True
    assert should_fail_job("any", error) is True


def test_incomplete_comment_is_visible() -> None:
    result = ReviewResult(
        verdict="error",
        summary="",
        incomplete_reason="Grok stopped because it hit max_turns.",
        stop_reason="max_turns",
    )
    body = format_incomplete_comment(
        result, scope="latest-commit", model="grok-4.6", run_url="https://example.test/run"
    )
    assert "Grok review incomplete" in body
    assert "max_turns" in body
    assert "latest-commit" in body
    assert "https://example.test/run" in body


def test_partial_review_is_visible_and_neutralizes_mentions() -> None:
    complete = ReviewResult(
        verdict="clean",
        summary="Looks good, @maintainers.",
        stop_reason="EndTurn",
    )
    result = mark_partial(complete, "Later files were omitted for @security.")
    body = format_review_body(
        result, scope="full-pr", model="grok-4.6", run_url="https://example.test/run"
    )
    assert result.verdict == "partial"
    assert "Grok PR review — partial" in body
    assert "[!WARNING]" in body
    assert "@\u200bmaintainers" in body
    assert "@\u200bsecurity" in body


def test_review_body_is_capped_by_aggregate_utf8_size() -> None:
    result = ReviewResult(
        verdict="issues",
        summary="🔍" * 8_000,
        issues=[
            Issue(
                severity="risk",
                path=f"src/file_{index}.py",
                line=index + 1,
                title=f"Finding {index}",
                detail="x" * 8_000,
            )
            for index in range(8)
        ],
        stop_reason="EndTurn",
    )

    body = format_review_body(result, scope="full-pr", model="grok-4.6", run_url="")

    assert len(body.encode("utf-8")) <= MAX_REVIEW_BODY_BYTES
    assert "Some finding detail was omitted" in body


def test_review_body_limit_does_not_change_normal_reviews() -> None:
    result = ReviewResult(verdict="clean", summary="Looks good.", stop_reason="EndTurn")

    body = format_review_body(result, scope="full-pr", model="grok-4.6", run_url="")

    assert "Looks good." in body
    assert "Some finding detail was omitted" not in body
