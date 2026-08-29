"""Versioned serialization for files shared by composite-action commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from grok_pr_review.config import SEVERITIES, Severity
from grok_pr_review.loop import MAX_ROUNDS_TRACKED, LoopState, _decode_finding
from grok_pr_review.scope import (
    CollectedReview,
    DiffKind,
    DiffPlan,
    Truncation,
    normalize_sha,
    parse_scope,
)

SCHEMA_VERSION = 1
_DIFF_KINDS = {"full-pr", "commit-range", "single-commit"}


class ArtifactError(ValueError):
    """Raised when a persisted action artifact is missing or invalid."""


@dataclass(frozen=True)
class ReviewContext:
    pr: dict[str, object]
    plan: DiffPlan
    truncated: bool
    original_bytes: int
    embedded_bytes: int
    max_diff_kb: int
    stubbed_paths: tuple[str, ...] = ()
    hard_cut: bool = False
    loop: LoopState | None = None

    @classmethod
    def from_collected(
        cls, collected: CollectedReview, loop: LoopState | None = None
    ) -> ReviewContext:
        truncation = collected.truncation
        return cls(
            pr=collected.pr,
            plan=collected.plan,
            truncated=truncation.truncated,
            original_bytes=truncation.original_bytes,
            embedded_bytes=truncation.embedded_bytes,
            max_diff_kb=truncation.max_diff_kb,
            stubbed_paths=truncation.stubbed_paths,
            hard_cut=truncation.hard_cut,
            loop=loop,
        )

    @classmethod
    def from_dict(cls, value: object) -> ReviewContext:
        data = _object(value, "review context")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ArtifactError(
                f"unsupported review context schema_version {version!r}; expected {SCHEMA_VERSION}"
            )

        pr = _object(data.get("pr"), "review context pr")
        number = pr.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ArtifactError("review context pr.number must be a positive integer")
        pr_head = _full_sha(pr.get("headRefOid"), "review context pr.headRefOid")
        pr["headRefOid"] = pr_head

        plan_data = _object(data.get("plan"), "review context plan")
        try:
            scope = parse_scope(_string(plan_data.get("scope"), "plan.scope"))
        except ValueError as exc:
            raise ArtifactError(str(exc)) from exc
        kind_value = _string(plan_data.get("kind"), "plan.kind")
        if kind_value not in _DIFF_KINDS:
            raise ArtifactError("plan.kind must be full-pr, commit-range, or single-commit")
        kind = cast(DiffKind, kind_value)
        from_sha = _optional_full_sha(plan_data.get("from_sha"), "plan.from_sha")
        to_sha = _full_sha(plan_data.get("to_sha"), "plan.to_sha")
        fallback_notice = _optional_string(plan_data.get("fallback_notice"), "plan.fallback_notice")
        if kind == "commit-range" and from_sha is None:
            raise ArtifactError("commit-range plan requires plan.from_sha")
        if kind != "commit-range" and from_sha is not None:
            raise ArtifactError(f"{kind} plan must not contain plan.from_sha")
        if scope == "full-pr" and kind != "full-pr":
            raise ArtifactError("full-pr scope requires full-pr plan.kind")
        if scope == "latest-commit" and kind == "full-pr":
            raise ArtifactError("latest-commit scope cannot contain full-pr plan.kind")
        if to_sha != pr_head:
            raise ArtifactError("plan.to_sha must match pr.headRefOid")

        truncation = _object(data.get("truncation"), "review context truncation")
        truncated = truncation.get("truncated")
        if not isinstance(truncated, bool):
            raise ArtifactError("truncation.truncated must be a boolean")
        original_bytes = _nonnegative_int(truncation.get("original_bytes"), "original_bytes")
        embedded_bytes = _nonnegative_int(truncation.get("embedded_bytes"), "embedded_bytes")
        max_diff_kb = _positive_int(truncation.get("max_diff_kb"), "max_diff_kb")
        if embedded_bytes > original_bytes:
            raise ArtifactError("truncation.embedded_bytes cannot exceed original_bytes")
        if embedded_bytes > max_diff_kb * 1024:
            raise ArtifactError("truncation.embedded_bytes exceeds max_diff_kb")
        if truncated and embedded_bytes >= original_bytes:
            raise ArtifactError("truncated context must omit at least one byte")
        if not truncated and embedded_bytes != original_bytes:
            raise ArtifactError("untruncated context must preserve every byte")
        stubbed_paths = _stubbed_paths(truncation.get("stubbed_paths", []))
        hard_cut = truncation.get("hard_cut", False)
        if not isinstance(hard_cut, bool):
            raise ArtifactError("truncation.hard_cut must be a boolean")
        if (stubbed_paths or hard_cut) and not truncated:
            raise ArtifactError("stubbed or hard-cut context must be marked truncated")

        return cls(
            pr=pr,
            plan=DiffPlan(
                scope=scope,
                kind=kind,
                from_sha=from_sha,
                to_sha=to_sha,
                fallback_notice=fallback_notice,
            ),
            truncated=truncated,
            original_bytes=original_bytes,
            embedded_bytes=embedded_bytes,
            max_diff_kb=max_diff_kb,
            stubbed_paths=stubbed_paths,
            hard_cut=hard_cut,
            loop=_loop_state(data.get("loop")),
        )

    @classmethod
    def read(cls, path: Path) -> ReviewContext:
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ArtifactError(f"could not read {path.name}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"could not parse {path.name}: {exc}") from exc
        return cls.from_dict(value)

    def write(self, path: Path) -> None:
        validated = self.from_dict(self.to_dict())
        try:
            serialized = json.dumps(validated.to_dict(), indent=2)
            path.write_text(serialized, encoding="utf-8")
        except (OSError, TypeError) as exc:
            raise ArtifactError(f"could not write {path.name}: {exc}") from exc

    def to_dict(self) -> dict[str, object]:
        loop_value: dict[str, object] | None = None
        if self.loop is not None:
            loop_value = {
                "mode": self.loop.mode,
                "round": self.loop.round_number,
                "severity_floor": self.loop.severity_floor,
                "escalated": self.loop.escalated,
                "retired": self.loop.retired,
                "findings": [
                    {
                        "id": finding.id,
                        "severity": finding.severity,
                        "path": finding.path,
                        "line": finding.line,
                        "title": finding.title,
                        "status": finding.status,
                    }
                    for finding in self.loop.prior_findings
                ],
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "pr": self.pr,
            "plan": {
                "scope": self.plan.scope,
                "kind": self.plan.kind,
                "from_sha": self.plan.from_sha,
                "to_sha": self.plan.to_sha,
                "fallback_notice": self.plan.fallback_notice,
            },
            "truncation": {
                "truncated": self.truncated,
                "original_bytes": self.original_bytes,
                "embedded_bytes": self.embedded_bytes,
                "max_diff_kb": self.max_diff_kb,
                "stubbed_paths": list(self.stubbed_paths),
                "hard_cut": self.hard_cut,
            },
            "loop": loop_value,
        }

    def to_collected(self, diff: str) -> CollectedReview:
        actual_bytes = len(diff.encode("utf-8"))
        if actual_bytes != self.embedded_bytes:
            raise ArtifactError(
                "diff.patch byte count does not match review-context.json "
                f"({actual_bytes} != {self.embedded_bytes})"
            )
        return CollectedReview(
            pr=self.pr,
            plan=self.plan,
            truncation=Truncation(
                text=diff,
                truncated=self.truncated,
                original_bytes=self.original_bytes,
                embedded_bytes=self.embedded_bytes,
                max_diff_kb=self.max_diff_kb,
                stubbed_paths=self.stubbed_paths,
                hard_cut=self.hard_cut,
            ),
        )

    @property
    def truncation_notice(self) -> str | None:
        return Truncation(
            text="",
            truncated=self.truncated,
            original_bytes=self.original_bytes,
            embedded_bytes=self.embedded_bytes,
            max_diff_kb=self.max_diff_kb,
            stubbed_paths=self.stubbed_paths,
            hard_cut=self.hard_cut,
        ).notice


def _loop_state(value: object) -> LoopState | None:
    if value is None:
        return None
    data = _object(value, "review context loop")
    mode = data.get("mode")
    if mode not in {"initial", "verify"}:
        raise ArtifactError("loop.mode must be initial or verify")
    round_number = data.get("round")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or not 1 <= round_number <= MAX_ROUNDS_TRACKED
    ):
        raise ArtifactError("loop.round must be a positive integer")
    floor = data.get("severity_floor")
    if floor not in SEVERITIES:
        raise ArtifactError("loop.severity_floor must be nit, risk, or bug")
    escalated = data.get("escalated")
    if not isinstance(escalated, bool):
        raise ArtifactError("loop.escalated must be a boolean")
    retired = data.get("retired")
    if isinstance(retired, bool) or not isinstance(retired, int) or retired < 0:
        raise ArtifactError("loop.retired must be a nonnegative integer")
    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        raise ArtifactError("loop.findings must be an array")
    findings = []
    for item in raw_findings:
        finding = _decode_finding(item)
        if finding is None:
            raise ArtifactError("loop.findings contains an invalid finding")
        findings.append(finding)
    return LoopState(
        mode=mode,
        round_number=round_number,
        severity_floor=cast(Severity, floor),
        escalated=escalated,
        retired=retired,
        prior_findings=tuple(findings),
    )


def _object(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ArtifactError(f"{name} must be a JSON object")
    return cast(dict[str, object], dict(value))


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _full_sha(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ArtifactError(f"{name} must be a full commit SHA")
    normalized = normalize_sha(value)
    if normalized is None or len(normalized) != 40:
        raise ArtifactError(f"{name} must be a full commit SHA")
    return normalized


def _optional_full_sha(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _full_sha(value, name)


def _stubbed_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ArtifactError("truncation.stubbed_paths must be an array")
    paths: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ArtifactError("truncation.stubbed_paths must contain non-empty strings")
        paths.append(item.strip())
    return tuple(paths)


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArtifactError(f"truncation.{name} must be a nonnegative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ArtifactError(f"truncation.{name} must be a positive integer")
    return value
