from __future__ import annotations

from grok_pr_review.loop import (
    Ledger,
    LedgerFinding,
    apply_round,
    build_loop_state,
    count_changed_lines,
    decide_loop_state,
    encode_ledger,
    extract_ledger,
    latest_ledger,
    ledger_for_sha,
    render_agent_context,
    severity_floor,
)
from grok_pr_review.result import Issue, Resolution, ReviewResult

SCHEDULE = ("nit", "risk", "bug")


def _finding(
    ident: str,
    severity: str = "bug",
    status: str = "open",
    title: str = "Broken thing",
) -> LedgerFinding:
    return LedgerFinding(
        id=ident,
        severity=severity,
        path="src/app.py",
        line=3,
        title=title,
        status=status,
    )


def test_ledger_round_trips_through_the_hidden_marker() -> None:
    ledger = Ledger(
        round_number=2,
        findings=(_finding("r1-1"), _finding("r1-2", "nit", "disputed", "Comment tone")),
    )
    marker = encode_ledger(ledger, repo="owner/repo", pr_number=7)
    assert marker.startswith("<!-- grok-review-ledger:v1:")
    body = f"## Grok PR review\n{marker}\n\nLooks good."
    decoded = extract_ledger(body, repo="owner/repo", pr_number=7)
    assert decoded == ledger
    assert extract_ledger(body, repo="other/repo", pr_number=7) is None
    assert extract_ledger(body, repo="owner/repo", pr_number=8) is None


def test_malformed_or_absent_ledgers_are_ignored() -> None:
    binding = {"repo": "owner/repo", "pr_number": 7}
    assert extract_ledger("no marker here", **binding) is None
    assert extract_ledger("<!-- grok-review-ledger:v1:!!!notbase64!!! -->", **binding) is None
    good = encode_ledger(Ledger(1, (_finding("r1-1"),)), repo="owner/repo", pr_number=7)
    later = encode_ledger(Ledger(3, (_finding("r1-1"),)), repo="owner/repo", pr_number=7)
    assert latest_ledger(["irrelevant", good, "codex review body", later], **binding) is not None
    found = latest_ledger([good, later], **binding)
    assert found is not None and found.round_number == 3


def test_decide_loop_state_resets_on_initial_and_advances_on_synchronize() -> None:
    ledger = Ledger(2, (_finding("r1-1"),))
    assert decide_loop_state(review_mode="auto", event_action="opened", ledger=None) == (
        "initial",
        1,
    )
    assert decide_loop_state(review_mode="auto", event_action="synchronize", ledger=ledger) == (
        "verify",
        3,
    )
    assert decide_loop_state(review_mode="auto", event_action="opened", ledger=ledger) == (
        "initial",
        1,
    )
    assert decide_loop_state(review_mode="initial", event_action="synchronize", ledger=ledger) == (
        "initial",
        1,
    )
    assert decide_loop_state(review_mode="verify", event_action="", ledger=ledger) == ("verify", 3)
    assert decide_loop_state(review_mode="verify", event_action="", ledger=None) == ("initial", 1)


def test_severity_floor_follows_the_schedule_and_repeats_the_last_entry() -> None:
    assert severity_floor(SCHEDULE, 1) == "nit"
    assert severity_floor(SCHEDULE, 2) == "risk"
    assert severity_floor(SCHEDULE, 3) == "bug"
    assert severity_floor(SCHEDULE, 9) == "bug"


def test_build_loop_state_retires_below_floor_findings_and_escalation_resets_floor() -> None:
    ledger = Ledger(
        2,
        (
            _finding("r1-1", "bug"),
            _finding("r1-2", "nit"),
            _finding("r1-3", "risk"),
        ),
    )
    state = build_loop_state(
        mode="verify", round_number=3, schedule=SCHEDULE, ledger=ledger, escalated=False
    )
    assert state.severity_floor == "bug"
    assert [finding.id for finding in state.prior_findings] == ["r1-1"]
    assert state.retired == 2

    escalated = build_loop_state(
        mode="verify", round_number=3, schedule=SCHEDULE, ledger=ledger, escalated=True
    )
    assert escalated.severity_floor == "nit"
    assert escalated.retired == 0


def test_apply_round_initial_assigns_ids_and_opens_the_ledger() -> None:
    state = build_loop_state(
        mode="initial", round_number=1, schedule=SCHEDULE, ledger=None, escalated=False
    )
    result = ReviewResult(
        verdict="issues",
        summary="Two problems.",
        issues=[
            Issue("bug", "src/app.py", 3, "Crash", "Boom."),
            Issue("nit", "src/app.py", 9, "Naming", "Meh."),
        ],
    )
    outcome = apply_round(state, result)
    assert [issue.id for issue in outcome.result.issues] == ["r1-1", "r1-2"]
    assert outcome.open_issue_count == 2
    assert outcome.open_bug_count == 1
    assert outcome.result.verdict == "issues"
    assert outcome.extra_lines == []
    marker = encode_ledger(outcome.ledger, repo="owner/repo", pr_number=7)
    assert extract_ledger(marker, repo="owner/repo", pr_number=7) == outcome.ledger


def test_apply_round_verify_resolves_carries_and_enforces_the_floor() -> None:
    ledger = Ledger(
        1,
        (
            _finding("r1-1", "bug", title="Fixed one"),
            _finding("r1-2", "bug", title="Still broken"),
            _finding("r1-3", "risk", title="Rebutted"),
            _finding("r1-4", "risk", title="Ignored"),
        ),
    )
    state = build_loop_state(
        mode="verify", round_number=2, schedule=SCHEDULE, ledger=ledger, escalated=False
    )
    result = ReviewResult(
        verdict="issues",
        summary="Verified fixes.",
        issues=[
            Issue("bug", "src/new.py", 5, "Regression in fix", "Introduced."),
            Issue("nit", "src/new.py", 8, "Below floor", "Dropped."),
        ],
        resolutions=[
            Resolution("r1-1", "fixed", "resolved"),
            Resolution("r1-2", "not_fixed", "still present"),
            Resolution("r1-3", "disputed", "agent rebuttal accepted"),
        ],
    )
    outcome = apply_round(state, result)

    statuses = {finding.id: finding.status for finding in outcome.ledger.findings}
    assert "r1-1" not in statuses
    assert statuses["r1-2"] == "open"
    assert statuses["r1-3"] == "disputed"
    assert statuses["r1-4"] == "open"
    assert statuses["r2-1"] == "open"
    assert [issue.id for issue in outcome.result.issues] == ["r2-1"]
    assert outcome.open_issue_count == 3
    assert outcome.open_bug_count == 2
    report = "\n".join(outcome.extra_lines)
    assert "Round 2 resolution" in report
    assert "`r1-1` fixed" in report
    assert "`r1-4` unaddressed" in report
    assert "1 new finding(s) below the `risk` severity floor" in report
    assert "Open findings after this round: 3 (2 bug-severity)" in report


def test_apply_round_reaches_clean_when_everything_resolves() -> None:
    ledger = Ledger(1, (_finding("r1-1", "bug"),))
    state = build_loop_state(
        mode="verify", round_number=2, schedule=SCHEDULE, ledger=ledger, escalated=False
    )
    result = ReviewResult(
        verdict="clean",
        summary="All fixed.",
        resolutions=[Resolution("r1-1", "fixed", "")],
    )
    outcome = apply_round(state, result)
    assert outcome.result.verdict == "clean"
    assert outcome.open_issue_count == 0
    assert outcome.open_bug_count == 0


def test_count_changed_lines_ignores_file_headers() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-old\n+new\n context\n"
    )
    assert count_changed_lines(diff) == 2


def test_render_agent_context_is_bounded_and_labeled() -> None:
    text = render_agent_context(
        [("r1-1", "codex-agent", "Not a real issue because X." + "y" * 3000)],
        [("nathan", "Looks fine to me.")],
    )
    assert "Reply to finding r1-1 (from codex-agent):" in text
    assert "…[clipped]" in text
    assert "PR comment (from nathan):" in text
    assert len(text.encode("utf-8")) <= 17_000


def test_encode_ledger_preserves_open_findings_or_fails_visibly() -> None:
    import pytest

    from grok_pr_review.scope import GhError

    open_findings = tuple(
        LedgerFinding(
            id=f"r1-{index + 1}",
            severity=("bug", "risk", "nit")[index % 3],
            path=f"src/file_{index}.py",
            line=index + 1,
            title="A very long finding title " * 10,
            status="open",
        )
        for index in range(120)
    )
    disputed = tuple(
        LedgerFinding(
            id=f"r2-{index + 1}",
            severity="nit",
            path="src/" + ("deep/" * 80) + f"d_{index}.py",
            line=1,
            title="Settled",
            status="disputed",
        )
        for index in range(150)
    )
    marker = encode_ledger(Ledger(5, open_findings + disputed), repo="owner/repo", pr_number=7)
    decoded = extract_ledger(marker, repo="owner/repo", pr_number=7)
    assert decoded is not None
    kept_open = [f for f in decoded.findings if f.status == "open"]
    assert len(kept_open) == 120  # every open finding survives; disputed may be dropped

    too_many_open = tuple(
        LedgerFinding(f"r1-{i + 1}", "bug", None, None, "t", "open") for i in range(301)
    )
    with pytest.raises(GhError, match="overflow"):
        encode_ledger(Ledger(5, too_many_open), repo="owner/repo", pr_number=7)

    oversized_open = tuple(
        LedgerFinding(f"r1-{i + 1}", "bug", "src/" + ("deep/" * 150) + f"{i}.py", 1, "t", "open")
        for i in range(300)
    )
    with pytest.raises(GhError, match="overflow"):
        encode_ledger(Ledger(5, oversized_open), repo="owner/repo", pr_number=7)


def test_latest_ledger_fails_on_a_corrupt_newest_marker_instead_of_rolling_back() -> None:
    import pytest

    from grok_pr_review.scope import GhError

    older = encode_ledger(Ledger(2, (_finding("r1-1"),)), repo="owner/repo", pr_number=7)
    corrupt = "<!-- grok-review-ledger:v1:AAAA -->"
    with pytest.raises(GhError, match="corrupted"):
        latest_ledger([older, corrupt], repo="owner/repo", pr_number=7)
    recovered = latest_ledger([corrupt, older], repo="owner/repo", pr_number=7)
    assert recovered is not None and recovered.round_number == 2


def test_ledger_for_sha_searches_all_bot_reviews_and_fails_closed() -> None:
    import pytest

    from grok_pr_review.scope import GhError

    old_sha = "a" * 40
    new_sha = "b" * 40
    old = encode_ledger(
        Ledger(1, (_finding("r1-1"),), reviewed_sha=old_sha),
        repo="owner/repo",
        pr_number=7,
    )
    new = encode_ledger(Ledger(2, (), reviewed_sha=new_sha), repo="owner/repo", pr_number=7)
    assert ledger_for_sha(
        [old, new], repo="owner/repo", pr_number=7, reviewed_sha=old_sha.upper()
    ) == Ledger(1, (_finding("r1-1"),), reviewed_sha=old_sha)
    assert ledger_for_sha([old, new], repo="owner/repo", pr_number=7, reviewed_sha="c" * 40) is None
    with pytest.raises(GhError, match="corrupted"):
        ledger_for_sha(
            [old, "<!-- grok-review-ledger:v1:AAAA -->"],
            repo="owner/repo",
            pr_number=7,
            reviewed_sha=old_sha,
        )


def test_decide_loop_state_clamps_the_round_counter() -> None:
    ledger = Ledger(999, (_finding("r1-1"),))
    assert decide_loop_state(review_mode="verify", event_action="", ledger=ledger) == (
        "verify",
        999,
    )
