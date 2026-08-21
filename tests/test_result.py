from __future__ import annotations

import json

from grok_pr_review.result import (
    ReviewResult,
    format_incomplete_comment,
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
