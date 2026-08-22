"""Parse Grok JSON output, compute verdicts, and format GitHub comments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

Verdict = Literal["clean", "issues", "partial", "error"]
FailOn = Literal["never", "bugs", "any"]
MAX_TURNS_REASONS = {"max_turn", "max_turn_requests", "max_turns", "max_turns_reached"}
SUCCESS_REASONS = {"end_turn"}
MAX_ISSUES = 100
MAX_SUMMARY_LENGTH = 8_000
MAX_TITLE_LENGTH = 300
MAX_DETAIL_LENGTH = 8_000
MAX_PATH_LENGTH = 1_000

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str | None
    line: int | None
    title: str
    detail: str

    @property
    def is_bug(self) -> bool:
        return self.severity == "bug"


@dataclass(frozen=True)
class ReviewResult:
    verdict: Verdict
    summary: str
    issues: list[Issue] = field(default_factory=list)
    incomplete_reason: str | None = None
    stop_reason: str | None = None
    partial_reason: str | None = None

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    @property
    def bug_count(self) -> int:
        return sum(1 for issue in self.issues if issue.is_bug)

    @property
    def incomplete(self) -> bool:
        return self.verdict == "error"


def parse_fail_on(value: str) -> FailOn:
    chosen = value.strip().lower() or "never"
    if chosen not in {"never", "bugs", "any"}:
        raise ValueError("fail_on must be never, bugs, or any")
    return chosen  # type: ignore[return-value]


def should_fail_job(fail_on: str, result: ReviewResult) -> bool:
    """fail_on=never never red-Xs the job for findings or an incomplete review."""
    policy = parse_fail_on(fail_on)
    if policy == "never":
        return False
    if policy == "bugs":
        return result.bug_count > 0
    return result.verdict in {"issues", "partial", "error"} or result.issue_count > 0


def parse_grok_output(raw: str, *, exit_code: int = 0) -> ReviewResult:
    """Read grok --output-format json (or raw text) and extract structured findings."""
    envelope, text = _split_envelope(raw)
    stop_reason = _stop_reason(envelope)
    findings = _extract_findings(text) or _extract_findings(raw)
    summary = ""
    issues: list[Issue] = []
    validation_error: str | None = None
    if findings is not None:
        try:
            summary, issues = _parse_findings(findings)
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
    )


def mark_partial(result: ReviewResult, reason: str) -> ReviewResult:
    """Mark an otherwise completed review as explicitly partial."""
    if result.verdict == "error":
        return result
    new_reason = reason.strip()
    combined_reason = (
        f"{result.partial_reason} {new_reason}" if result.partial_reason else new_reason
    )
    return ReviewResult(
        verdict="partial",
        summary=result.summary,
        issues=result.issues,
        stop_reason=result.stop_reason,
        partial_reason=combined_reason,
    )


def format_review_body(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
    heading = "Grok PR review"
    if result.verdict == "clean":
        heading += " — clean"
    elif result.verdict == "issues":
        heading += f" — {result.issue_count} issue(s)"
    elif result.verdict == "partial":
        heading += f" — partial ({result.issue_count} issue(s))"
    lines = [
        f"## {heading}",
        "",
        neutralize_mentions(result.summary.strip()) or "Review completed.",
        "",
        f"- Scope: `{scope}`",
        f"- Model: `{model}`",
        f"- Issues: {result.issue_count} ({result.bug_count} bug-severity)",
    ]
    if result.partial_reason:
        lines.extend(["", "> [!WARNING]", f"> {neutralize_mentions(result.partial_reason)}"])
    if run_url:
        lines.append(f"- Workflow run: {run_url}")
    if result.issues:
        lines.extend(["", "### Findings", ""])
        for issue in result.issues:
            location = issue.path or "(no path)"
            if issue.line is not None:
                location = f"{location}:{issue.line}"
            lines.append(
                f"- **{neutralize_mentions(issue.title)}** (`{issue.severity}`) — "
                f"`{neutralize_mentions(location)}`"
            )
            lines.append(f"  {neutralize_mentions(issue.detail)}")
    return "\n".join(lines).rstrip() + "\n"


def format_incomplete_comment(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
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
        lines.append(f"- Grok stop reason: `{result.stop_reason}`")
    if run_url:
        lines.append(f"- Workflow run: {run_url}")
    return "\n".join(lines).rstrip() + "\n"


def inline_review_comments(result: ReviewResult) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for issue in result.issues:
        if not issue.path or issue.line is None:
            continue
        comments.append(
            {
                "path": issue.path,
                "line": issue.line,
                "side": "RIGHT",
                "body": (
                    f"**{neutralize_mentions(issue.title)}** (`{issue.severity}`)\n\n"
                    f"{neutralize_mentions(issue.detail)}"
                ),
            }
        )
    return comments


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


def _parse_findings(findings: dict[str, Any]) -> tuple[str, list[Issue]]:
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
    return summary, issues


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


def neutralize_mentions(value: str) -> str:
    return value.replace("@", "@\u200b")
