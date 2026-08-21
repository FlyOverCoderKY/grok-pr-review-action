"""Parse Grok JSON output, compute verdicts, and format GitHub comments."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Verdict = Literal["clean", "issues", "error"]
FailOn = Literal["never", "bugs", "any"]
MAX_TURNS_REASONS = {"max_turns", "max_turn", "max-turns"}

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
    return result.verdict in {"issues", "error"} or result.issue_count > 0


def parse_grok_output(raw: str, *, exit_code: int = 0) -> ReviewResult:
    """Read grok --output-format json (or raw text) and extract structured findings."""
    envelope, text = _split_envelope(raw)
    stop_reason = _stop_reason(envelope)
    findings = _extract_findings(text) or _extract_findings(raw)

    if _is_max_turns(stop_reason):
        return ReviewResult(
            verdict="error",
            summary=_summary_from(findings),
            issues=_issues_from(findings),
            incomplete_reason="Grok stopped because it hit max_turns.",
            stop_reason=stop_reason,
        )

    if findings is None:
        reason = "Grok returned no structured JSON findings."
        if exit_code != 0:
            reason += f" grok exit code was {exit_code}."
        return ReviewResult(
            verdict="error",
            summary="",
            issues=[],
            incomplete_reason=reason,
            stop_reason=stop_reason,
        )

    issues = _issues_from(findings)
    summary = _summary_from(findings)
    return ReviewResult(
        verdict="issues" if issues else "clean",
        summary=summary,
        issues=issues,
        stop_reason=stop_reason,
    )


def format_review_body(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
    heading = "Grok PR review"
    if result.verdict == "clean":
        heading += " — clean"
    elif result.verdict == "issues":
        heading += f" — {result.issue_count} issue(s)"
    lines = [
        f"## {heading}",
        "",
        result.summary.strip() or "Review completed.",
        "",
        f"- Scope: `{scope}`",
        f"- Model: `{model}`",
        f"- Issues: {result.issue_count} ({result.bug_count} bug-severity)",
    ]
    if run_url:
        lines.append(f"- Workflow run: {run_url}")
    if result.issues:
        lines.extend(["", "### Findings", ""])
        for issue in result.issues:
            location = issue.path or "(no path)"
            if issue.line is not None:
                location = f"{location}:{issue.line}"
            lines.append(f"- **{issue.title}** (`{issue.severity}`) — {location}")
            lines.append(f"  {issue.detail}")
    return "\n".join(lines).rstrip() + "\n"


def format_incomplete_comment(result: ReviewResult, *, scope: str, model: str, run_url: str) -> str:
    reason = result.incomplete_reason or "The review did not finish with structured findings."
    lines = [
        "## Grok review incomplete",
        "",
        reason,
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
                "body": f"**{issue.title}** (`{issue.severity}`)\n\n{issue.detail}",
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
    normalized = stop_reason.lower().replace("-", "_")
    return normalized in MAX_TURNS_REASONS


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


def _summary_from(findings: dict[str, Any] | None) -> str:
    if not findings:
        return ""
    summary = findings.get("summary")
    return summary.strip() if isinstance(summary, str) else ""


def _issues_from(findings: dict[str, Any] | None) -> list[Issue]:
    if not findings:
        return []
    raw_issues = findings.get("issues")
    if not isinstance(raw_issues, list):
        return []
    issues: list[Issue] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "risk").strip().lower()
        if severity not in {"bug", "risk", "nit"}:
            severity = "risk"
        path = item.get("path") or item.get("file")
        line = item.get("line")
        issues.append(
            Issue(
                severity=severity,
                path=path.strip() if isinstance(path, str) and path.strip() else None,
                line=line if isinstance(line, int) and line > 0 else None,
                title=str(item.get("title") or "Finding").strip(),
                detail=str(item.get("detail") or item.get("body") or "").strip(),
            )
        )
    return issues
