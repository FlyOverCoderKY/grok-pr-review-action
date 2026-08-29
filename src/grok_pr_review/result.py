"""Parse Grok JSON output, compute verdicts, and format GitHub comments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any, Literal

from grok_pr_review.config import parse_fail_on

Verdict = Literal["clean", "issues", "partial", "error"]
MAX_TURNS_REASONS = {"max_turn", "max_turn_requests", "max_turns", "max_turns_reached"}
SUCCESS_REASONS = {"end_turn"}
MAX_ISSUES = 100
MAX_COVERAGE_ENTRIES = 500
# Keep this aligned with loop.MAX_LEDGER_FINDINGS: a valid ledger must be
# possible to resolve completely in one verification response.
MAX_RESOLUTIONS = 300
MAX_SUMMARY_LENGTH = 8_000
MAX_TITLE_LENGTH = 300
MAX_DETAIL_LENGTH = 8_000
MAX_PATH_LENGTH = 1_000
MAX_FINDING_ID_LENGTH = 32
RESOLUTION_STATUSES = ("fixed", "not_fixed", "fixed_incorrectly", "disputed")
SEVERITY_RANK = {"nit": 0, "risk": 1, "bug": 2}
MAX_GITHUB_BODY_BYTES = 60_000
TARGET_GITHUB_BODY_BYTES = 58_000

_BODY_TRUNCATION_NOTICE = (
    "\n\n> [!WARNING]\n> Some text was omitted to stay within GitHub's body limit."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str | None
    line: int | None
    title: str
    detail: str
    id: str | None = None

    @property
    def is_bug(self) -> bool:
        return self.severity == "bug"


@dataclass(frozen=True)
class Resolution:
    """The model's verdict on one prior-round finding during a verify round."""

    id: str
    status: str  # fixed | not_fixed | fixed_incorrectly | disputed
    note: str


@dataclass(frozen=True)
class ReviewResult:
    verdict: Verdict
    summary: str
    issues: list[Issue] = field(default_factory=list)
    incomplete_reason: str | None = None
    stop_reason: str | None = None
    partial_reason: str | None = None
    resolutions: list[Resolution] = field(default_factory=list)
    coverage: list[tuple[str, int]] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def bug_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_bug)

    @property
    def incomplete(self) -> bool:
        return self.verdict == "error"


def should_fail_job(
    fail_on: str,
    result: ReviewResult,
    *,
    open_bug_count: int | None = None,
    open_issue_count: int | None = None,
) -> bool:
    """Operational errors always fail; fail_on controls completed review findings.

    In verify rounds the open counts (carried-over plus new findings) drive the
    policy so an unfixed bug from an earlier round still fails fail_on=bugs.
    """
    policy = parse_fail_on(fail_on)
    bug_count = result.bug_count if open_bug_count is None else open_bug_count
    issue_count = result.issue_count if open_issue_count is None else open_issue_count
    if result.verdict == "error":
        return True
    if policy == "never":
        return False
    if policy == "bugs":
        return bug_count > 0
    return result.verdict in {"issues", "partial"} or issue_count > 0


def parse_grok_output(raw: str, *, exit_code: int = 0) -> ReviewResult:
    """Read grok --output-format json (or raw text) and extract structured findings."""
    envelope, text = _split_envelope(raw)
    stop_reason = _stop_reason(envelope)
    findings = _extract_findings(text) or _extract_findings(raw)
    summary = ""
    issues: list[Issue] = []
    resolutions: list[Resolution] = []
    coverage: list[tuple[str, int]] = []
    validation_error: str | None = None
    if findings is not None:
        try:
            summary, issues, resolutions, coverage = _parse_findings(findings)
        except ValueError as exc:
            validation_error = str(exc)

    if exit_code != 0:
        reason = f"Grok exited unsuccessfully with code {exit_code}."
        if _is_max_turns(stop_reason):
            reason = "Grok stopped because it hit max_turns."
        return ReviewResult(
            verdict="error",
            summary=summary,
            issues=issues,
            incomplete_reason=reason,
            stop_reason=stop_reason,
        )

    if isinstance(envelope, dict) and envelope.get("type") == "error":
        message = envelope.get("message")
        detail = (
            message.strip() if isinstance(message, str) and message.strip() else "unknown error"
        )
        return ReviewResult(
            verdict="error",
            summary=summary,
            issues=issues,
            incomplete_reason=f"Grok returned an error response: {detail}",
            stop_reason=stop_reason,
        )

    if not _is_success(stop_reason):
        displayed = stop_reason or "missing"
        return ReviewResult(
            verdict="error",
            summary=summary,
            issues=issues,
            incomplete_reason=f"Grok stop reason was {displayed}; expected EndTurn.",
            stop_reason=stop_reason,
        )

    if findings is None:
        return ReviewResult(
            verdict="error",
            summary="",
            issues=[],
            incomplete_reason="Grok returned no structured JSON findings.",
            stop_reason=stop_reason,
        )

    if validation_error:
        return ReviewResult(
            verdict="error",
            summary="",
            issues=[],
            incomplete_reason=f"Grok returned invalid structured findings: {validation_error}",
            stop_reason=stop_reason,
        )

    return ReviewResult(
        verdict="issues" if issues else "clean",
        summary=summary,
        issues=issues,
        stop_reason=stop_reason,
        resolutions=resolutions,
        coverage=coverage,
    )


def mark_partial(result: ReviewResult, reason: str) -> ReviewResult:
    """Mark an otherwise completed review as explicitly partial."""
    if result.verdict == "error":
        return result
    new_reason = reason.strip()
    combined_reason = (
        f"{result.partial_reason} {new_reason}" if result.partial_reason else new_reason
    )
    return replace(result, verdict="partial", partial_reason=combined_reason)


def format_review_body(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
    return format_review_body_parts(result, scope=scope, model=model, run_url=run_url)[0]


def format_review_body_parts(
    result: ReviewResult,
    *,
    scope: str,
    model: str,
    run_url: str,
    hidden_marker: str | None = None,
    extra_lines: list[str] | None = None,
) -> list[str]:
    """Render every finding across bounded GitHub bodies without dropping detail.

    hidden_marker is emitted directly under the heading so body-tail truncation
    can never cut it; extra_lines land after the metadata block (e.g. the
    verify-round resolution report).
    """
    heading = "Grok PR review"
    if result.verdict == "clean":
        heading += " — clean"
    elif result.verdict == "issues":
        heading += f" — {result.issue_count} issue(s)"
    elif result.verdict == "partial":
        heading += f" — partial ({result.issue_count} issue(s))"
    first_lines = [f"## {heading}"]
    if hidden_marker:
        first_lines.append(hidden_marker)
    first_lines.extend(
        [
            "",
            neutralize_mentions(result.summary.strip()) or "Review completed.",
            "",
            f"- Scope: `{scope}`",
            f"- Model: `{model}`",
            f"- Issues: {result.issue_count} ({result.bug_count} bug-severity)",
        ]
    )
    if result.partial_reason:
        first_lines.extend(["", "> [!WARNING]", f"> {neutralize_mentions(result.partial_reason)}"])
    if run_url:
        first_lines.append(f"- Workflow run: {run_url}")
    if extra_lines:
        first_lines.extend(["", *extra_lines])
    if not result.issues:
        return [limit_github_body("\n".join(first_lines).rstrip() + "\n")]

    first_lines.extend(["", "### Findings", ""])
    return _chunk_issue_bodies(
        first_lines,
        result.issues,
        continuation_lines=["## Grok PR review — continued", "", "### Findings", ""],
    )


def format_incomplete_comment(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
    return format_incomplete_comment_parts(result, scope=scope, model=model, run_url=run_url)[0]


def format_incomplete_comment_parts(
    result: ReviewResult,
    *,
    scope: str,
    model: str,
    run_url: str,
) -> list[str]:
    """Render an incomplete result, retaining findings recovered before the failure."""
    reason = result.incomplete_reason or "The review did not finish with structured findings."
    lines = [
        "## Grok review incomplete",
        "",
        neutralize_mentions(reason),
        "",
        "This is not a silent failure. Re-run the workflow or raise `max_turns` "
        "if the agent ran out of turns.",
        "",
        f"- Scope: `{scope}`",
        f"- Model: `{model}`",
        f"- Verdict: `{result.verdict}`",
    ]
    if result.stop_reason:
        lines.append(f"- Grok stop reason: `{_inline_code(result.stop_reason)}`")
    if run_url:
        lines.append(f"- Workflow run: {run_url}")
    if not result.issues:
        return [limit_github_body("\n".join(lines).rstrip() + "\n")]
    lines.extend(["", "### Findings recovered before the failure", ""])
    return _chunk_issue_bodies(
        lines,
        result.issues,
        continuation_lines=[
            "## Grok incomplete review — continued",
            "",
            "### Findings recovered before the failure",
            "",
        ],
    )


def format_pipeline_failure_comment(*, stage: str, reason: str, run_url: str) -> str:
    """Visible PR comment for a pipeline step that failed before Grok could run."""
    lines = [
        "## Grok review incomplete",
        "",
        f"The review pipeline failed during {stage}, before Grok could run: "
        f"{neutralize_mentions(reason)}",
        "",
        "This is not a silent failure. Re-run the workflow once the underlying "
        "problem is resolved.",
    ]
    if run_url:
        lines.extend(["", f"- Workflow run: {run_url}"])
    return "\n".join(lines).rstrip() + "\n"


def _chunk_issue_bodies(
    first_lines: list[str],
    issues: list[Issue],
    *,
    continuation_lines: list[str],
) -> list[str]:
    part_lines: list[list[str]] = []
    current = first_lines
    prefix_length = len(current)
    for issue in issues:
        block = _format_issue_block(issue)
        candidate = "\n".join([*current, *block]).rstrip() + "\n"
        if len(candidate.encode("utf-8")) > TARGET_GITHUB_BODY_BYTES and (
            len(current) > prefix_length or not part_lines
        ):
            part_lines.append(current)
            current = list(continuation_lines)
            prefix_length = len(current)
        current.extend(block)
    part_lines.append(current)

    total = len(part_lines)
    bodies: list[str] = []
    for index, lines in enumerate(part_lines, start=1):
        if total > 1:
            lines = [*lines, "", f"_Part {index} of {total}; all findings are preserved._"]
        bodies.append(limit_github_body("\n".join(lines).rstrip() + "\n"))
    return bodies


def inline_review_comments(
    result: ReviewResult, *, allowed_paths: set[str] | None = None
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for issue in result.issues:
        if not issue.path or issue.line is None:
            continue
        if allowed_paths is not None and issue.path not in allowed_paths:
            continue
        marker = f"{finding_marker(issue.id)}\n" if issue.id else ""
        comments.append(
            {
                "path": issue.path,
                "line": issue.line,
                "side": "RIGHT",
                "body": (
                    f"{marker}**{neutralize_mentions(issue.title)}** (`{issue.severity}`)\n\n"
                    f"{neutralize_mentions(issue.detail)}"
                ),
            }
        )
    return comments


def finding_marker(finding_id: str | None) -> str:
    """Invisible marker tying an inline comment to a ledger finding id."""
    return f"<!-- grok-finding:{finding_id} -->"


def extract_finding_marker(body: str) -> str | None:
    match = _FINDING_MARKER_RE.search(body)
    return match.group(1) if match else None


_FINDING_MARKER_RE = re.compile(r"<!-- grok-finding:([A-Za-z0-9_-]{1,32}) -->")


def _split_envelope(raw: str) -> tuple[dict[str, Any] | None, str]:
    stripped = raw.strip()
    if not stripped:
        return None, ""
    try:
        parsed: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return None, raw
    if isinstance(parsed, dict):
        text = parsed.get("text") or parsed.get("result") or parsed.get("message") or ""
        if isinstance(text, str) and text.strip():
            return parsed, text
        return parsed, raw
    return None, raw


def _stop_reason(envelope: dict[str, Any] | None) -> str | None:
    if not envelope:
        return None
    for key in ("stopReason", "stop_reason"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _is_max_turns(stop_reason: str | None) -> bool:
    if not stop_reason:
        return False
    normalized = _normalize_stop_reason(stop_reason)
    return normalized in MAX_TURNS_REASONS


def _is_success(stop_reason: str | None) -> bool:
    return bool(stop_reason and _normalize_stop_reason(stop_reason) in SUCCESS_REASONS)


def _normalize_stop_reason(stop_reason: str) -> str:
    with_word_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", stop_reason.strip())
    return with_word_boundaries.lower().replace("-", "_")


def _extract_findings(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    fenced = _FENCE_RE.findall(text)
    candidates = list(fenced)
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            candidates.append(json.dumps(loaded))
    except json.JSONDecodeError:
        candidates.extend(_json_objects(text))

    for blob in reversed(candidates):
        try:
            data = json.loads(blob) if isinstance(blob, str) else blob
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and _looks_like_findings(data):
            return data
    return None


def _json_objects(text: str) -> list[str]:
    objects: list[str] = []
    decoder = json.JSONDecoder()
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            _obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        objects.append(text[start:end])
        index = end
    return objects


def _looks_like_findings(data: dict[str, Any]) -> bool:
    return "issues" in data or "summary" in data


def _parse_findings(
    findings: dict[str, Any],
) -> tuple[str, list[Issue], list[Resolution], list[tuple[str, int]]]:
    summary_value = findings.get("summary")
    if not isinstance(summary_value, str) or not summary_value.strip():
        raise ValueError("summary must be a non-empty string")
    summary = summary_value.strip()
    if len(summary) > MAX_SUMMARY_LENGTH:
        raise ValueError(f"summary exceeds {MAX_SUMMARY_LENGTH} characters")

    raw_issues = findings.get("issues")
    if not isinstance(raw_issues, list):
        raise ValueError("issues must be an array")
    if len(raw_issues) > MAX_ISSUES:
        raise ValueError(f"issues exceeds the limit of {MAX_ISSUES}")

    issues: list[Issue] = []
    for index, item in enumerate(raw_issues):
        if not isinstance(item, dict):
            raise ValueError(f"issues[{index}] must be an object")

        severity_value = item.get("severity")
        if not isinstance(severity_value, str):
            raise ValueError(f"issues[{index}].severity must be a string")
        severity = severity_value.strip().lower()
        if severity not in {"bug", "risk", "nit"}:
            raise ValueError(f"issues[{index}].severity is invalid")

        path = item.get("path") or item.get("file")
        line = item.get("line")
        title = item.get("title")
        detail = item.get("detail") or item.get("body")
        if path is not None and (not isinstance(path, str) or not _valid_review_path(path)):
            raise ValueError(f"issues[{index}].path must be a safe relative path or null")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line <= 0):
            raise ValueError(f"issues[{index}].line must be a positive integer or null")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"issues[{index}].title must be a non-empty string")
        if len(title.strip()) > MAX_TITLE_LENGTH:
            raise ValueError(f"issues[{index}].title exceeds {MAX_TITLE_LENGTH} characters")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError(f"issues[{index}].detail must be a non-empty string")
        if len(detail.strip()) > MAX_DETAIL_LENGTH:
            raise ValueError(f"issues[{index}].detail exceeds {MAX_DETAIL_LENGTH} characters")

        issues.append(
            Issue(
                severity=severity,
                path=path.strip() if isinstance(path, str) else None,
                line=line,
                title=title.strip(),
                detail=detail.strip(),
            )
        )
    return summary, issues, _parse_resolutions(findings), _parse_coverage(findings)


def _parse_coverage(findings: dict[str, Any]) -> list[tuple[str, int]]:
    raw = findings.get("coverage")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("coverage must be an array or absent")
    if len(raw) > MAX_COVERAGE_ENTRIES:
        raise ValueError(f"coverage exceeds the limit of {MAX_COVERAGE_ENTRIES}")
    entries: list[tuple[str, int]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"coverage[{index}] must be an object")
        path = item.get("path")
        count = item.get("findings")
        if not isinstance(path, str) or not _valid_review_path(path):
            raise ValueError(f"coverage[{index}].path must be a safe relative path")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"coverage[{index}].findings must be a nonnegative integer")
        normalized = path.strip()
        if normalized in seen:
            raise ValueError(f"coverage lists {normalized!r} more than once")
        seen.add(normalized)
        entries.append((normalized, count))
    return entries


def paths_outside_embed(result: ReviewResult, diff_paths: set[str]) -> list[str]:
    """Finding paths that are not in the embedded diff.

    Out-of-embed citations are valid: Grok may report blast-radius or stale-doc
    issues in files the change did not edit, and a truncated embed may omit PR
    files that still belong in the review. Callers must not treat these as a
    parse error.
    """
    return sorted(
        {issue.path for issue in result.issues if issue.path and issue.path not in diff_paths}
    )


def validate_coverage(result: ReviewResult, diff_paths: set[str]) -> str | None:
    """Reject an initial review whose coverage manifest does not account for the embed.

    Coverage is required only for files that appear in the embedded diff.
    Findings on other paths are kept and do not fail this check, and coverage
    entries for files outside the embedded diff are ignored: a tool-assisted
    review of a truncated dense PR legitimately accounts for files the embed
    omitted. Per-file counts that do not match the findings kept for those
    files are not a parse error; use coverage_count_mismatches() to surface a
    note while keeping a completed verdict.
    """
    if not diff_paths:
        return None
    covered = dict(result.coverage)
    if not covered:
        return "coverage is missing; the manifest must account for every diff file"
    missing = sorted(diff_paths - set(covered))
    if missing:
        named = ", ".join(missing[:5])
        return f"coverage does not account for {len(missing)} diff file(s): {named}"
    return None


def coverage_count_mismatches(result: ReviewResult, diff_paths: set[str]) -> list[str]:
    """Per-file coverage counts that do not match reported in-diff findings.

    These are not parse errors. Callers must keep the recovered findings and
    post a completed verdict (issues / clean / partial), not verdict=error.
    """
    covered = dict(result.coverage)
    reported: dict[str, int] = {}
    for issue in result.issues:
        if issue.path and issue.path in diff_paths:
            reported[issue.path] = reported.get(issue.path, 0) + 1
    notes: list[str] = []
    for path, count in covered.items():
        if path not in diff_paths:
            continue
        actual = reported.get(path, 0)
        if actual != count:
            notes.append(
                f"coverage claims {count} finding(s) in {path!r} but {actual} were reported"
            )
    return notes


def note_coverage_count_mismatch(result: ReviewResult, notes: list[str]) -> ReviewResult:
    """Keep a completed review when coverage counts disagree with reported findings."""
    if result.verdict == "error" or not notes:
        return result
    reason = "Coverage count mismatch; recovered findings were kept. " + "; ".join(notes)
    combined = f"{result.partial_reason} {reason}" if result.partial_reason else reason
    return replace(result, partial_reason=combined)


def _parse_resolutions(findings: dict[str, Any]) -> list[Resolution]:
    raw = findings.get("resolutions")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("resolutions must be an array or absent")
    if len(raw) > MAX_RESOLUTIONS:
        raise ValueError(f"resolutions exceeds the limit of {MAX_RESOLUTIONS}")
    resolutions: list[Resolution] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"resolutions[{index}] must be an object")
        ident = item.get("id")
        if (
            not isinstance(ident, str)
            or not ident.strip()
            or len(ident.strip()) > MAX_FINDING_ID_LENGTH
        ):
            raise ValueError(f"resolutions[{index}].id must be a short non-empty string")
        status_value = item.get("status")
        if not isinstance(status_value, str):
            raise ValueError(f"resolutions[{index}].status must be a string")
        status = status_value.strip().lower().replace("-", "_").replace(" ", "_")
        if status not in RESOLUTION_STATUSES:
            raise ValueError(f"resolutions[{index}].status is invalid")
        note = item.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError(f"resolutions[{index}].note must be a string or null")
        note_text = (note or "").strip()
        if len(note_text) > MAX_DETAIL_LENGTH:
            raise ValueError(f"resolutions[{index}].note exceeds {MAX_DETAIL_LENGTH} characters")
        resolutions.append(Resolution(id=ident.strip(), status=status, note=note_text))
    return resolutions


def _valid_review_path(value: str) -> bool:
    path = value.strip()
    if (
        not path
        or len(path) > MAX_PATH_LENGTH
        or "\\" in path
        or "`" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and all(part not in {"", ".", ".."} for part in parsed.parts)


def _format_issue_block(issue: Issue) -> list[str]:
    location = issue.path or "(no path)"
    if issue.line is not None:
        location = f"{location}:{issue.line}"
    return [
        f"- **{neutralize_mentions(issue.title)}** (`{issue.severity}`) — "
        f"`{neutralize_mentions(location)}`",
        f"  {neutralize_mentions(issue.detail)}",
    ]


def limit_github_body(body: str) -> str:
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_GITHUB_BODY_BYTES:
        return body

    suffix = (_BODY_TRUNCATION_NOTICE + "\n").encode("utf-8")
    prefix = encoded[: MAX_GITHUB_BODY_BYTES - len(suffix)].decode("utf-8", errors="ignore")
    last_newline = prefix.rfind("\n")
    if last_newline >= max(0, len(prefix) - 2_000):
        prefix = prefix[:last_newline]
    return prefix.rstrip() + suffix.decode("utf-8")


def _inline_code(value: str) -> str:
    return neutralize_mentions(value).replace("`", "'").replace("\r", " ").replace("\n", " ")


def neutralize_mentions(value: str) -> str:
    return value.replace("@", "@\u200b")
