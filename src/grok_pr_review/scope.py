"""Review-scope planning: full PR vs latest-commit, plus diff truncation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

ScopeName = Literal["full-pr", "latest-commit"]
DiffKind = Literal["full-pr", "commit-range", "single-commit"]

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_ZERO_RE = re.compile(r"^0+$")

MISSING_BEFORE_NOTICE = (
    "The before SHA was missing (first push, force-push, or workflow_dispatch). "
    "This prompt embeds only the single latest commit on the PR head. "
    "This is not a full-PR review and the full pull request diff was not fetched."
)

COMPARE_FAILED_NOTICE = (
    "The before...after compare failed (missing history or a force-push). "
    "This prompt embeds only the single latest commit on the PR head. "
    "This is not a full-PR review and the full pull request diff was not fetched."
)


class GhError(RuntimeError):
    """A GitHub CLI/API call failed."""


class GitHubPort(Protocol):
    def pr_view(self, number: int) -> dict[str, object]: ...

    def pr_diff(self, number: int) -> str: ...

    def compare_diff(self, before: str, after: str) -> str: ...

    def commit_diff(self, sha: str) -> str: ...


@dataclass(frozen=True)
class DiffRequest:
    scope: ScopeName
    before_sha: str | None
    after_sha: str | None
    head_sha: str | None


@dataclass(frozen=True)
class DiffPlan:
    scope: ScopeName
    kind: DiffKind
    from_sha: str | None
    to_sha: str | None
    fallback_notice: str | None

    def as_single_commit_fallback(self, notice: str) -> DiffPlan:
        return DiffPlan(
            scope=self.scope,
            kind="single-commit",
            from_sha=None,
            to_sha=self.to_sha,
            fallback_notice=notice,
        )


@dataclass(frozen=True)
class Truncation:
    text: str
    truncated: bool
    original_bytes: int
    embedded_bytes: int
    max_diff_kb: int

    @property
    def notice(self) -> str | None:
        if not self.truncated:
            return None
        original_kb = self.original_bytes / 1024
        embedded_kb = self.embedded_bytes / 1024
        return (
            f"Diff truncated from {original_kb:.1f} KB to {embedded_kb:.1f} KB "
            f"(max_diff_kb={self.max_diff_kb}). Later files/hunks are missing."
        )


@dataclass(frozen=True)
class CollectedReview:
    pr: dict[str, object]
    plan: DiffPlan
    truncation: Truncation

    @property
    def diff(self) -> str:
        return self.truncation.text


def parse_scope(value: str) -> ScopeName:
    scope = value.strip().lower()
    if scope in {"full-pr", "latest-commit"}:
        return scope  # type: ignore[return-value]
    raise ValueError("review_scope must be 'full-pr' or 'latest-commit'")


def normalize_sha(value: str | None) -> str | None:
    if value is None:
        return None
    sha = value.strip()
    if sha == "" or _ZERO_RE.fullmatch(sha) or not _SHA_RE.fullmatch(sha):
        return None
    return sha.lower()


def plan_diff(request: DiffRequest) -> DiffPlan:
    """Decide which git range to embed. latest-commit never plans a full-PR diff."""
    scope = request.scope
    head = normalize_sha(request.head_sha)
    after = normalize_sha(request.after_sha) or head
    before = normalize_sha(request.before_sha)

    if scope == "full-pr":
        return DiffPlan(
            scope=scope,
            kind="full-pr",
            from_sha=None,
            to_sha=after,
            fallback_notice=None,
        )

    if before and after and before != after:
        return DiffPlan(
            scope=scope,
            kind="commit-range",
            from_sha=before,
            to_sha=after,
            fallback_notice=None,
        )

    if after is None:
        raise ValueError(
            "latest-commit requires a head SHA (github.event.after or "
            "pull_request.head.sha). Refusing to fall back to the full PR diff."
        )

    return DiffPlan(
        scope=scope,
        kind="single-commit",
        from_sha=None,
        to_sha=after,
        fallback_notice=MISSING_BEFORE_NOTICE,
    )


def truncate_diff(diff: str, max_diff_kb: int) -> Truncation:
    if max_diff_kb <= 0:
        raise ValueError("max_diff_kb must be a positive integer")
    limit = max_diff_kb * 1024
    data = diff.encode("utf-8")
    if len(data) <= limit:
        return Truncation(
            text=diff,
            truncated=False,
            original_bytes=len(data),
            embedded_bytes=len(data),
            max_diff_kb=max_diff_kb,
        )
    cut = _cut_diff_at_boundary(data, limit)
    text = cut.decode("utf-8", errors="ignore")
    return Truncation(
        text=text,
        truncated=True,
        original_bytes=len(data),
        embedded_bytes=len(text.encode("utf-8")),
        max_diff_kb=max_diff_kb,
    )


def _cut_diff_at_boundary(data: bytes, limit: int) -> bytes:
    """Prefer a file/hunk boundary cut, but never discard most of the byte budget."""
    prefix = data[:limit]
    minimum = limit // 2
    boundary = max(prefix.rfind(b"\ndiff --git "), prefix.rfind(b"\n@@ "))
    if boundary + 1 > minimum:
        return prefix[: boundary + 1]
    newline = prefix.rfind(b"\n")
    if newline + 1 > minimum:
        return prefix[: newline + 1]
    return prefix


def changed_paths(diff_text: str) -> set[str]:
    """File paths touched by a unified diff (old and new sides, no /dev/null)."""
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("--- a/"):
            paths.add(line[6:].strip())
        elif line.startswith("+++ b/"):
            paths.add(line[6:].strip())
    return {path for path in paths if path}


def fetch_scoped_diff(
    pr_number: int,
    plan: DiffPlan,
    github: GitHubPort,
) -> tuple[str, DiffPlan]:
    """Return (diff, maybe-updated plan). latest-commit never calls pr_diff."""
    if plan.kind == "full-pr":
        return github.pr_diff(pr_number), plan

    if plan.kind == "commit-range":
        if plan.from_sha is None or plan.to_sha is None:
            raise GhError("commit-range plan is missing SHAs")
        try:
            return github.compare_diff(plan.from_sha, plan.to_sha), plan
        except GhError:
            fallback = plan.as_single_commit_fallback(COMPARE_FAILED_NOTICE)
            if not fallback.to_sha:
                raise GhError("latest-commit fallback is missing a head SHA") from None
            return github.commit_diff(fallback.to_sha), fallback

    if not plan.to_sha:
        raise GhError("latest-commit is missing a head SHA; refusing full-PR fallback")
    return github.commit_diff(plan.to_sha), plan


def collect_review_material(
    *,
    pr_number: int,
    request: DiffRequest,
    max_diff_kb: int,
    github: GitHubPort,
) -> CollectedReview:
    pr = github.pr_view(pr_number)
    head_from_pr = normalize_sha(_as_str(pr.get("headRefOid")))
    full_pr = request.scope == "full-pr"
    if full_pr and head_from_pr is None:
        raise GhError("PR head SHA is missing from the PR metadata; retry the review")
    plan = plan_diff(
        DiffRequest(
            scope=request.scope,
            before_sha=None if full_pr else request.before_sha,
            after_sha=None if full_pr else request.after_sha,
            head_sha=head_from_pr if full_pr else request.head_sha or head_from_pr,
        )
    )
    raw, plan = fetch_scoped_diff(pr_number, plan, github)
    if full_pr:
        confirmed_pr = github.pr_view(pr_number)
        confirmed_head = normalize_sha(_as_str(confirmed_pr.get("headRefOid")))
        if confirmed_head is None:
            raise GhError("PR head SHA is missing from the PR metadata; retry the review")
        if confirmed_head != plan.to_sha:
            raise GhError("PR head changed while collecting the full-PR diff; retry the review")
        pr = confirmed_pr
    return CollectedReview(pr=pr, plan=plan, truncation=truncate_diff(raw, max_diff_kb))


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
