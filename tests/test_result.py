from __future__ import annotations

import json

from grok_pr_review.result import (
    MAX_GITHUB_BODY_BYTES,
    MAX_RESOLUTIONS,
    Issue,
    ReviewResult,
    format_incomplete_comment,
    format_incomplete_comment_parts,
    format_review_body,
    format_review_body_parts,
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


def test_operational_errors_always_fail_and_policy_only_controls_findings() -> None:
    issues = parse_grok_output(json.dumps({"text": json.dumps(FINDINGS), "stopReason": "end_turn"}))
    error = ReviewResult(verdict="error", summary="", incomplete_reason="no JSON")
    assert should_fail_job("never", issues) is False
    assert should_fail_job("never", error) is True
    assert should_fail_job("bugs", issues) is True
    assert should_fail_job("bugs", error) is True
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


def test_review_bodies_are_capped_without_omitting_validated_findings() -> None:
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

    bodies = format_review_body_parts(result, scope="full-pr", model="grok-4.6", run_url="")
    aggregate = "\n".join(bodies)

    assert len(bodies) > 1
    assert all(len(body.encode("utf-8")) <= MAX_GITHUB_BODY_BYTES for body in bodies)
    for index in range(8):
        assert f"Finding {index}" in aggregate
        assert f"src/file_{index}.py" in aggregate
    assert "Some text was omitted" not in aggregate
    assert format_review_body(result, scope="full-pr", model="grok-4.6", run_url="") == bodies[0]


def test_incomplete_bodies_preserve_findings_and_bound_untrusted_text() -> None:
    result = ReviewResult(
        verdict="error",
        summary="",
        incomplete_reason="failed @maintainers",
        issues=[
            Issue("risk", f"src/file_{index}.py", index + 1, f"Finding {index}", "x" * 8_000)
            for index in range(8)
        ],
        stop_reason="bad`reason\nnext",
    )

    bodies = format_incomplete_comment_parts(result, scope="full-pr", model="grok-4.6", run_url="")
    aggregate = "\n".join(bodies)

    assert all(len(body.encode("utf-8")) <= MAX_GITHUB_BODY_BYTES for body in bodies)
    assert "@\u200bmaintainers" in aggregate
    assert "bad'reason next" in aggregate
    for index in range(8):
        assert f"Finding {index}" in aggregate


def test_incomplete_comment_caps_a_huge_runtime_error() -> None:
    result = ReviewResult(
        verdict="error",
        summary="",
        incomplete_reason="🔍" * 20_000,
    )

    body = format_incomplete_comment(result, scope="full-pr", model="grok-4.6", run_url="")

    assert len(body.encode("utf-8")) <= MAX_GITHUB_BODY_BYTES
    assert "Some text was omitted" in body


def test_review_body_limit_does_not_change_normal_reviews() -> None:
    result = ReviewResult(verdict="clean", summary="Looks good.", stop_reason="EndTurn")

    body = format_review_body(result, scope="full-pr", model="grok-4.6", run_url="")

    assert "Looks good." in body
    assert "Some finding detail was omitted" not in body


def test_resolutions_parse_with_findings_and_fail_closed_when_invalid() -> None:
    payload = {
        "summary": "Verified.",
        "issues": [],
        "resolutions": [
            {"id": "r1-1", "status": "Fixed", "note": "done"},
            {"id": "r1-2", "status": "fixed-incorrectly", "note": None},
        ],
    }
    envelope = {"text": json.dumps(payload), "stopReason": "EndTurn"}
    result = parse_grok_output(json.dumps(envelope))
    assert result.verdict == "clean"
    assert [(r.id, r.status) for r in result.resolutions] == [
        ("r1-1", "fixed"),
        ("r1-2", "fixed_incorrectly"),
    ]

    bad = dict(payload, resolutions=[{"id": "r1-1", "status": "maybe"}])
    envelope = {"text": json.dumps(bad), "stopReason": "EndTurn"}
    result = parse_grok_output(json.dumps(envelope))
    assert result.verdict == "error"
    assert result.incomplete_reason is not None
    assert "invalid structured findings" in result.incomplete_reason


def test_hidden_marker_sits_under_the_heading_with_extra_lines_after_metadata() -> None:
    result = ReviewResult(verdict="clean", summary="Fine.", stop_reason="EndTurn")
    body = format_review_body_parts(
        result,
        scope="full-pr",
        model="grok-4.6",
        run_url="",
        hidden_marker="<!-- test-marker -->",
        extra_lines=["### Round 2 resolution", "- ✅ `r1-1` fixed — **Crash**"],
    )[0]
    lines = body.splitlines()
    assert lines[0].startswith("## Grok PR review")
    assert lines[1] == "<!-- test-marker -->"
    assert "### Round 2 resolution" in body
    assert "`r1-1` fixed" in body


def test_inline_comments_carry_extractable_finding_markers() -> None:
    from grok_pr_review.result import extract_finding_marker, inline_review_comments

    result = ReviewResult(
        verdict="issues",
        summary="One.",
        issues=[Issue("bug", "src/app.py", 3, "Crash", "Boom.", id="r1-1")],
    )
    comments = inline_review_comments(result)
    assert len(comments) == 1
    assert extract_finding_marker(comments[0]["body"]) == "r1-1"
    assert extract_finding_marker("no marker") is None


def test_coverage_allows_findings_outside_the_embedded_diff() -> None:
    from grok_pr_review.result import paths_outside_embed, validate_coverage

    result = ReviewResult(
        verdict="issues",
        summary="In-diff bug plus stale docs.",
        issues=[
            Issue("bug", "src/app.py", 3, "Crash", "In the diff."),
            Issue("nit", "DOCS/README.md", 1, "Stale README", "Docs were not edited."),
            Issue("nit", "DOCS/code-map.md", 4, "Stale map", "Map still names the old API."),
        ],
        coverage=[("src/app.py", 1)],
    )
    assert validate_coverage(result, {"src/app.py"}) is None
    assert paths_outside_embed(result, {"src/app.py"}) == [
        "DOCS/README.md",
        "DOCS/code-map.md",
    ]


def test_coverage_count_mismatch_keeps_findings_and_is_not_error() -> None:
    from grok_pr_review.result import (
        coverage_count_mismatches,
        note_coverage_count_mismatch,
        validate_coverage,
    )

    path = "packages/engine/scripts/rules-dispatch.mjs"
    for claimed, reported in ((6, 7), (7, 8), (3, 2)):
        payload = {
            "summary": f"Coverage claimed {claimed} but {reported} findings were listed.",
            "issues": [
                {
                    "severity": "risk",
                    "path": path,
                    "line": index + 1,
                    "title": f"Finding {index + 1}",
                    "detail": f"Recovered finding {index + 1} on {path}.",
                }
                for index in range(reported)
            ],
            "coverage": [{"path": path, "findings": claimed}],
        }
        parsed = parse_grok_output(
            json.dumps({"text": json.dumps(payload), "stopReason": "end_turn"})
        )
        assert parsed.verdict == "issues"
        assert parsed.stop_reason == "end_turn"
        assert len(parsed.issues) == reported
        assert validate_coverage(parsed, {path}) is None

        notes = coverage_count_mismatches(parsed, {path})
        result = note_coverage_count_mismatch(parsed, notes)
        assert notes == [
            f"coverage claims {claimed} finding(s) in {path!r} but {reported} were reported"
        ]
        assert result.verdict == "issues"
        assert result.verdict != "error"
        assert result.incomplete is False
        assert result.incomplete_reason is None
        assert [issue.title for issue in result.issues] == [
            f"Finding {index + 1}" for index in range(reported)
        ]
        assert result.partial_reason is not None
        assert "recovered findings were kept" in result.partial_reason
        assert f"claims {claimed}" in result.partial_reason
        assert f"{reported} were reported" in result.partial_reason


def test_coverage_count_mismatch_does_not_drop_out_of_diff_findings() -> None:
    from grok_pr_review.result import (
        coverage_count_mismatches,
        note_coverage_count_mismatch,
        validate_coverage,
    )

    result = ReviewResult(
        verdict="issues",
        summary="Claimed count does not match listed embed findings.",
        issues=[
            Issue("bug", "src/app.py", 3, "Crash", "In the diff."),
            Issue("nit", "DOCS/README.md", 1, "Stale README", "Docs were not edited."),
        ],
        coverage=[("src/app.py", 2)],
        stop_reason="end_turn",
    )
    assert validate_coverage(result, {"src/app.py"}) is None
    noted = note_coverage_count_mismatch(
        result, coverage_count_mismatches(result, {"src/app.py"})
    )
    assert noted.verdict == "issues"
    assert [issue.path for issue in noted.issues] == ["src/app.py", "DOCS/README.md"]
    assert noted.partial_reason is not None
    assert "claims 2" in noted.partial_reason
    assert "1 were reported" in noted.partial_reason


def test_coverage_count_mismatch_note_does_not_rewrite_an_error() -> None:
    from grok_pr_review.result import note_coverage_count_mismatch

    error = ReviewResult(
        verdict="error",
        summary="One real bug.",
        issues=[Issue("bug", "src/app.py", 3, "Crash", "In the diff.")],
        incomplete_reason="Grok returned no structured JSON findings.",
        stop_reason="end_turn",
    )
    noted = note_coverage_count_mismatch(
        error, ["coverage claims 6 finding(s) in 'src/app.py' but 7 were reported"]
    )
    assert noted is error
    assert noted.verdict == "error"
    assert noted.incomplete_reason == "Grok returned no structured JSON findings."


def test_coverage_still_requires_every_embedded_diff_file() -> None:
    from grok_pr_review.result import validate_coverage

    result = ReviewResult(
        verdict="issues",
        summary="Missed an embed file.",
        issues=[Issue("nit", "DOCS/README.md", 1, "Stale", "Outside the PR.")],
        coverage=[("src/app.py", 0)],
    )
    error = validate_coverage(result, {"src/app.py", "src/other.py"})
    assert error is not None
    assert "does not account" in error
    assert "src/other.py" in error


def test_inline_comments_skip_paths_outside_the_embed() -> None:
    from grok_pr_review.result import inline_review_comments

    result = ReviewResult(
        verdict="issues",
        summary="One in-diff, one blast-radius.",
        issues=[
            Issue("bug", "src/app.py", 3, "Crash", "Boom.", id="r1-1"),
            Issue("nit", "DOCS/README.md", 1, "Stale", "Update the docs.", id="r1-2"),
        ],
    )
    comments = inline_review_comments(result, allowed_paths={"src/app.py"})
    assert [comment["path"] for comment in comments] == ["src/app.py"]


def test_resolution_capacity_matches_the_maximum_ledger_backlog() -> None:
    from grok_pr_review.loop import MAX_LEDGER_FINDINGS

    assert MAX_RESOLUTIONS == MAX_LEDGER_FINDINGS
    resolutions = [
        {"id": f"r1-{index}", "status": "fixed", "note": "resolved"}
        for index in range(1, MAX_LEDGER_FINDINGS + 1)
    ]
    envelope = {
        "text": json.dumps(
            {"summary": "Verified the full backlog.", "issues": [], "resolutions": resolutions}
        ),
        "stopReason": "EndTurn",
    }

    result = parse_grok_output(json.dumps(envelope))

    assert result.verdict == "clean"
    assert len(result.resolutions) == MAX_LEDGER_FINDINGS
