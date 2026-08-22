"""Review-loop state: the bot's private findings ledger and round/floor logic.

The ledger is bot-internal memory embedded invisibly in the bot's own posted
review bodies. It is not a contract with the fixing agent or any other review
bot: agents respond through ordinary commits and comment-thread replies, and
the model adjudicates dispositions from those universal signals each round.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, replace

from grok_pr_review.config import SEVERITIES, Severity
from grok_pr_review.result import (
    SEVERITY_RANK,
    Issue,
    ReviewResult,
    _valid_review_path,
    neutralize_mentions,
)

LEDGER_PREFIX = "<!-- grok-review-ledger:v1:"
LEDGER_SUFFIX = " -->"
MAX_LEDGER_FINDINGS = 300
MAX_LEDGER_BYTES = 40_000
MAX_ROUNDS_TRACKED = 999
MAX_REPLY_CHARS = 2_000
MAX_REPLIES_BYTES = 16_000

_FINDING_ID_RE = re.compile(r"^r\d{1,3}-\d{1,3}$")
_LEDGER_RE = re.compile(re.escape(LEDGER_PREFIX) + r"([A-Za-z0-9+/=]+)" + re.escape(LEDGER_SUFFIX))

_STATUS_ICONS = {
    "fixed": "✅",
    "not_fixed": "❌",
    "fixed_incorrectly": "⚠️",
    "disputed": "🤝",
    "unaddressed": "⏳",
}


@dataclass(frozen=True)
class LedgerFinding:
    id: str
    severity: str  # nit | risk | bug
    path: str | None
    line: int | None
    title: str
    status: str  # open | disputed


@dataclass(frozen=True)
class Ledger:
    round_number: int
    findings: tuple[LedgerFinding, ...]
    reviewed_sha: str = ""


@dataclass(frozen=True)
class LoopState:
    """The loop position of the current run, persisted in review-context.json."""

    mode: str  # initial | verify
    round_number: int
    severity_floor: Severity
    escalated: bool
    retired: int
    prior_findings: tuple[LedgerFinding, ...]

    @property
    def open_prior(self) -> tuple[LedgerFinding, ...]:
        return tuple(finding for finding in self.prior_findings if finding.status == "open")

    @property
    def disputed_prior(self) -> tuple[LedgerFinding, ...]:
        return tuple(finding for finding in self.prior_findings if finding.status == "disputed")


@dataclass(frozen=True)
class RoundOutcome:
    ledger: Ledger
    result: ReviewResult
    extra_lines: list[str]
    open_issue_count: int
    open_bug_count: int


def decide_loop_state(
    *,
    review_mode: str,
    event_action: str,
    ledger: Ledger | None,
) -> tuple[str, int]:
    """Return (mode, round_number). An initial review always resets the loop."""
    if review_mode == "initial" or ledger is None:
        return "initial", 1
    next_round = min(ledger.round_number + 1, MAX_ROUNDS_TRACKED)
    if review_mode == "verify":
        return "verify", next_round
    if event_action.strip().lower() == "synchronize":
        return "verify", next_round
    return "initial", 1


def severity_floor(schedule: tuple[Severity, ...], round_number: int) -> Severity:
    index = min(max(round_number, 1), len(schedule)) - 1
    return schedule[index]


def meets_floor(severity: str, floor: str) -> bool:
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(floor, 0)


def count_changed_lines(diff_text: str) -> int:
    count = 0
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            count += 1
    return count


def build_loop_state(
    *,
    mode: str,
    round_number: int,
    schedule: tuple[Severity, ...],
    ledger: Ledger | None,
    escalated: bool,
) -> LoopState:
    floor: Severity = (
        "nit" if mode == "initial" or escalated else severity_floor(schedule, round_number)
    )
    prior = ledger.findings if ledger is not None and mode == "verify" else ()
    kept = tuple(finding for finding in prior if meets_floor(finding.severity, floor))
    return LoopState(
        mode=mode,
        round_number=round_number,
        severity_floor=floor,
        escalated=escalated,
        retired=len(prior) - len(kept),
        prior_findings=kept,
    )


def apply_round(state: LoopState, result: ReviewResult) -> RoundOutcome:
    """Fold a completed round into the ledger and produce the posting artifacts."""
    resolutions = {resolution.id: resolution for resolution in result.resolutions}
    carried: list[LedgerFinding] = []
    resolution_lines: list[str] = []

    for finding in state.open_prior:
        resolution = resolutions.get(finding.id)
        status = resolution.status if resolution else "unaddressed"
        note = resolution.note if resolution else ""
        resolution_lines.append(_resolution_line(finding, status, note))
        if status == "fixed":
            continue
        if status == "disputed":
            carried.append(replace(finding, status="disputed"))
            continue
        carried.append(finding)

    carried.extend(state.disputed_prior)

    kept_issues: list[Issue] = []
    dropped_below_floor = 0
    for issue in result.issues:
        if not meets_floor(issue.severity, state.severity_floor):
            dropped_below_floor += 1
            continue
        kept_issues.append(replace(issue, id=f"r{state.round_number}-{len(kept_issues) + 1}"))
    carried.extend(
        LedgerFinding(
            id=issue.id or "",
            severity=issue.severity,
            path=issue.path,
            line=issue.line,
            title=issue.title,
            status="open",
        )
        for issue in kept_issues
    )

    ledger = Ledger(round_number=state.round_number, findings=tuple(carried))
    open_findings = [finding for finding in ledger.findings if finding.status == "open"]
    open_bug_count = sum(1 for finding in open_findings if finding.severity == "bug")

    updated = replace(result, issues=kept_issues)
    if updated.verdict in {"clean", "issues"}:
        updated = replace(updated, verdict="issues" if open_findings else "clean")

    extra_lines: list[str] = []
    if state.mode == "verify":
        extra_lines.append(f"### Round {state.round_number} resolution")
        extra_lines.extend(resolution_lines or ["- No prior findings were open."])
        if state.retired:
            extra_lines.append(
                f"- {state.retired} lower-severity finding(s) from earlier rounds retired "
                f"(severity floor is now `{state.severity_floor}`)."
            )
        if dropped_below_floor:
            extra_lines.append(
                f"- {dropped_below_floor} new finding(s) below the `{state.severity_floor}` "
                "severity floor were not recorded."
            )
        if state.escalated:
            extra_lines.append(
                "- This push was large, so it received a full-severity review "
                "instead of a fix-verification pass."
            )
        extra_lines.append(
            f"- Open findings after this round: {len(open_findings)} "
            f"({open_bug_count} bug-severity)."
        )

    return RoundOutcome(
        ledger=ledger,
        result=updated,
        extra_lines=extra_lines,
        open_issue_count=len(open_findings),
        open_bug_count=open_bug_count,
    )


def encode_ledger(ledger: Ledger, *, repo: str, pr_number: int) -> str:
    """Encode a marker bound to this repo/PR that is guaranteed to decode.

    Findings are kept in priority order (open bugs, open risks, open nits,
    then disputed) and trimmed deterministically to both the finding-count
    and byte limits, so a valid next-round load can never be bricked by an
    oversized or over-full marker.
    """
    findings = _trim_findings(list(ledger.findings))
    while True:
        encoded = _encode(ledger, findings, repo=repo, pr_number=pr_number)
        if len(encoded) <= MAX_LEDGER_BYTES or not findings:
            break
        findings = findings[:-1]
    token = encoded[len(LEDGER_PREFIX) : -len(LEDGER_SUFFIX)]
    if _decode(token, repo=repo, pr_number=pr_number) is None:
        encoded = _encode(ledger, [], repo=repo, pr_number=pr_number)
    return encoded


def _trim_findings(findings: list[LedgerFinding]) -> list[LedgerFinding]:
    def priority(finding: LedgerFinding) -> tuple[int, int]:
        status_rank = 0 if finding.status == "open" else 1
        return (status_rank, -SEVERITY_RANK.get(finding.severity, 0))

    ordered = sorted(findings, key=priority)
    clipped = [replace(finding, title=finding.title[:80].strip() or "…") for finding in ordered]
    return clipped[:MAX_LEDGER_FINDINGS]


def _encode(ledger: Ledger, findings: list[LedgerFinding], *, repo: str, pr_number: int) -> str:
    payload = {
        "repo": repo,
        "pr": pr_number,
        "sha": ledger.reviewed_sha,
        "round": ledger.round_number,
        "findings": [
            {
                "id": finding.id,
                "severity": finding.severity,
                "path": finding.path,
                "line": finding.line,
                "title": finding.title,
                "status": finding.status,
            }
            for finding in findings
        ],
    }
    compact = json.dumps(payload, separators=(",", ":"))
    token = base64.b64encode(compact.encode("utf-8")).decode("ascii")
    return f"{LEDGER_PREFIX}{token}{LEDGER_SUFFIX}"


def has_ledger_marker(body: str) -> bool:
    return LEDGER_PREFIX in body


def extract_ledger(body: str, *, repo: str, pr_number: int) -> Ledger | None:
    """Decode the newest valid ledger marker bound to this repo/PR, else None."""
    for match in reversed(list(_LEDGER_RE.finditer(body))):
        ledger = _decode(match.group(1), repo=repo, pr_number=pr_number)
        if ledger is not None:
            return ledger
    return None


def latest_ledger(bodies: list[str], *, repo: str, pr_number: int) -> Ledger | None:
    for body in reversed(bodies):
        ledger = extract_ledger(body, repo=repo, pr_number=pr_number)
        if ledger is not None:
            return ledger
    return None


def _decode(token: str, *, repo: str, pr_number: int) -> Ledger | None:
    try:
        payload = json.loads(base64.b64decode(token, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("repo") != repo or payload.get("pr") != pr_number:
        return None
    sha = payload.get("sha")
    if not isinstance(sha, str) or (sha != "" and not re.fullmatch(r"[0-9a-f]{40}", sha)):
        return None
    round_number = payload.get("round")
    raw_findings = payload.get("findings")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or not 1 <= round_number <= MAX_ROUNDS_TRACKED
        or not isinstance(raw_findings, list)
        or len(raw_findings) > MAX_LEDGER_FINDINGS
    ):
        return None
    findings: list[LedgerFinding] = []
    for item in raw_findings:
        finding = _decode_finding(item)
        if finding is None:
            return None
        findings.append(finding)
    return Ledger(round_number=round_number, findings=tuple(findings), reviewed_sha=sha)


def _decode_finding(item: object) -> LedgerFinding | None:
    if not isinstance(item, dict):
        return None
    ident = item.get("id")
    severity = item.get("severity")
    path = item.get("path")
    line = item.get("line")
    title = item.get("title")
    status = item.get("status")
    if not isinstance(ident, str) or not _FINDING_ID_RE.fullmatch(ident):
        return None
    if severity not in SEVERITIES:
        return None
    if path is not None and (not isinstance(path, str) or not _valid_review_path(path)):
        return None
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line <= 0):
        return None
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        return None
    if status not in {"open", "disputed"}:
        return None
    return LedgerFinding(
        id=ident,
        severity=severity,
        path=path,
        line=line,
        title=title.strip(),
        status=status,
    )


def render_agent_context(
    finding_replies: list[tuple[str, str, str]],
    issue_comments: list[tuple[str, str]],
) -> str:
    """Render comment-thread replies and PR comments into a bounded prompt block."""
    lines: list[str] = []
    for finding_id, login, body in finding_replies:
        lines.append(f"Reply to finding {finding_id} (from {login}):")
        lines.append(_clip_reply(body))
        lines.append("")
    for login, body in issue_comments:
        lines.append(f"PR comment (from {login}):")
        lines.append(_clip_reply(body))
        lines.append("")
    text = "\n".join(lines).strip()
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_REPLIES_BYTES:
        text = encoded[:MAX_REPLIES_BYTES].decode("utf-8", errors="ignore")
        text += "\n…[additional replies omitted]"
    return text


def _clip_reply(body: str) -> str:
    text = body.strip()
    if len(text) > MAX_REPLY_CHARS:
        return text[:MAX_REPLY_CHARS] + "…[clipped]"
    return text


def _resolution_line(finding: LedgerFinding, status: str, note: str) -> str:
    icon = _STATUS_ICONS.get(status, "⏳")
    label = status.replace("_", " ")
    text = f"- {icon} `{finding.id}` {label} — **{neutralize_mentions(finding.title)}**"
    if note:
        text += f": {neutralize_mentions(note)}"
    return text
