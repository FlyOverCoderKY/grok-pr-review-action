"""Review-scope planning: full PR vs latest-commit, plus diff truncation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
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
    stubbed_paths: tuple[str, ...] = ()
    hard_cut: bool = False

    @property
    def notice(self) -> str | None:
        if not self.truncated:
            return None
        original_kb = self.original_bytes / 1024
        embedded_kb = self.embedded_bytes / 1024
        sentences = [
            f"Diff truncated from {original_kb:.1f} KB to {embedded_kb:.1f} KB "
            f"(max_diff_kb={self.max_diff_kb})."
        ]
        if self.stubbed_paths:
            named = ", ".join(self.stubbed_paths[:5])
            if len(self.stubbed_paths) > 5:
                named += f", and {len(self.stubbed_paths) - 5} more"
            sentences.append(
                f"{len(self.stubbed_paths)} generated or large data file(s) are "
                f"embedded as header-only stubs: {named}."
            )
        if self.stubbed_paths and not self.hard_cut:
            sentences.append("Every file is present; only stubbed hunks are omitted.")
        else:
            sentences.append("Later files/hunks are missing.")
        return " ".join(sentences)


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


# Diff triage for over-cap diffs: hunks of these files are replaced with
# header-only stubs before falling back to a positional cut, so hand-written
# source stays embedded. Never applied to a diff that already fits.
GENERATED_BASENAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "packages.lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
        "deno.lock",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "pdm.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        "gradle.lockfile",
        "flake.lock",
    }
)
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map", ".snap", ".pb.go", "_pb2.py", "_pb2.pyi")
GENERATED_DIR_NAMES = frozenset({"node_modules", "vendor", "__generated__", ".yarn"})
# Data-ish files are stubbed only when their own diff is large; a small
# hand-maintained JSON or type stub is embedded like any other source.
LARGE_DATA_SUFFIXES = (".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".svg", ".d.ts")
LARGE_DATA_STUB_BYTES = 65_536


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
    working, stubbed_paths = _stub_generated_hunks(diff)
    working_data = working.encode("utf-8")
    if len(working_data) <= limit:
        return Truncation(
            text=working,
            truncated=True,
            original_bytes=len(data),
            embedded_bytes=len(working_data),
            max_diff_kb=max_diff_kb,
            stubbed_paths=stubbed_paths,
        )
    cut = _cut_diff_at_boundary(working_data, limit)
    text = cut.decode("utf-8", errors="ignore")
    return Truncation(
        text=text,
        truncated=True,
        original_bytes=len(data),
        embedded_bytes=len(text.encode("utf-8")),
        max_diff_kb=max_diff_kb,
        stubbed_paths=stubbed_paths,
        hard_cut=True,
    )


def _stub_generated_hunks(diff: str) -> tuple[str, tuple[str, ...]]:
    """Replace each generated/large-data file section with a header-only stub.

    The `diff --git` header line survives, so the file stays in the embedded
    diff's path set (and therefore in the coverage contract); only its hunks
    are dropped, with a visible note telling the model what was omitted.
    """
    pieces: list[str] = []
    stubbed: list[str] = []
    for header, body in _file_sections(diff):
        if header is None:
            pieces.append(body)
            continue
        path = _header_path(header)
        section = header + body
        if path is None or not _should_stub(path, len(section.encode("utf-8"))):
            pieces.append(section)
            continue
        hunks = sum(1 for line in body.splitlines() if line.startswith("@@"))
        omitted = len(body.encode("utf-8"))
        stubbed.append(path)
        pieces.append(
            header + f"# {hunks} hunk(s), {omitted} bytes omitted (generated or large data "
            "file); this file is still part of the diff - account for it in "
            "coverage and inspect it with tools if it needs review\n"
        )
    return "".join(pieces), tuple(stubbed)


def _file_sections(diff: str) -> list[tuple[str | None, str]]:
    """Split a unified diff into (header line, section body) pairs.

    Text before the first `diff --git` header is returned as (None, text).
    """
    sections: list[tuple[str | None, str]] = []
    header: str | None = None
    body: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if header is not None or body:
                sections.append((header, "".join(body)))
            header = line
            body = []
        else:
            body.append(line)
    if header is not None or body:
        sections.append((header, "".join(body)))
    return sections


def _header_path(header: str) -> str | None:
    remainder = header[len("diff --git a/") :] if header.startswith("diff --git a/") else ""
    _left, separator, right = remainder.rstrip("\n").rpartition(" b/")
    return right if separator and right else None


def _should_stub(path: str, section_bytes: int) -> bool:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    basename = parts[-1] if parts else lowered
    if basename in GENERATED_BASENAMES:
        return True
    if basename.endswith(GENERATED_SUFFIXES):
        return True
    if any(part in GENERATED_DIR_NAMES for part in parts[:-1]):
        return True
    return basename.endswith(LARGE_DATA_SUFFIXES) and section_bytes > LARGE_DATA_STUB_BYTES


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
    """File paths touched by a unified diff, old and new sides.

    Reads the `diff --git` headers as well as the `---`/`+++` and rename
    lines, so pure renames, mode-only changes, and binary files (which have
    no `---`/`+++` pair) are still accounted for.
    """
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            remainder = line[len("diff --git a/") :]
            left, separator, right = remainder.rpartition(" b/")
            if separator:
                paths.add(left)
                paths.add(right)
        elif line.startswith(("--- a/", "+++ b/")):
            paths.add(line[6:])
        elif line.startswith("rename from "):
            paths.add(line[len("rename from ") :])
        elif line.startswith("rename to "):
            paths.add(line[len("rename to ") :])
    return {path.strip() for path in paths if path.strip()}


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
