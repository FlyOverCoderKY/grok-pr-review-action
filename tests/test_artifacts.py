from __future__ import annotations

from pathlib import Path

import pytest

from grok_pr_review.artifacts import ArtifactError, ReviewContext
from grok_pr_review.scope import CollectedReview, DiffPlan, Truncation

HEAD = "a" * 40


def _collected(diff: str = "diff\n") -> CollectedReview:
    size = len(diff.encode("utf-8"))
    return CollectedReview(
        pr={"number": 7, "headRefOid": HEAD, "title": "Change"},
        plan=DiffPlan("full-pr", "full-pr", None, HEAD, None),
        truncation=Truncation(diff, False, size, size, 300),
    )


def test_review_context_round_trips_with_a_versioned_schema(tmp_path: Path) -> None:
    collected = _collected("diff 🔍\n")
    path = tmp_path / "review-context.json"

    ReviewContext.from_collected(collected).write(path)
    restored = ReviewContext.read(path)

    assert restored.to_collected(collected.diff) == collected
    assert restored.to_dict()["schema_version"] == 1


def test_review_context_round_trips_stubbed_truncation(tmp_path: Path) -> None:
    diff = "diff --git a/src/app.py b/src/app.py\n+x\n"
    size = len(diff.encode("utf-8"))
    collected = CollectedReview(
        pr={"number": 7, "headRefOid": HEAD, "title": "Change"},
        plan=DiffPlan("full-pr", "full-pr", None, HEAD, None),
        truncation=Truncation(
            text=diff,
            truncated=True,
            original_bytes=size + 90_000,
            embedded_bytes=size,
            max_diff_kb=300,
            stubbed_paths=("package-lock.json", "src/data/rule-coverage.json"),
        ),
    )
    path = tmp_path / "review-context.json"

    ReviewContext.from_collected(collected).write(path)
    restored = ReviewContext.read(path)

    assert restored.to_collected(diff) == collected
    assert restored.stubbed_paths == ("package-lock.json", "src/data/rule-coverage.json")
    assert restored.hard_cut is False
    notice = restored.truncation_notice or ""
    assert "package-lock.json" in notice
    assert "Every file is present" in notice


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema_version=99), "schema_version"),
        (lambda data: data["pr"].update(number=0), "positive integer"),
        (lambda data: data["pr"].update(headRefOid="short"), "full commit SHA"),
        (lambda data: data["plan"].update(to_sha="b" * 40), "must match"),
        (lambda data: data["truncation"].update(embedded_bytes=1), "preserve every byte"),
        (
            lambda data: data["truncation"].update(stubbed_paths=["package-lock.json"]),
            "must be marked truncated",
        ),
        (lambda data: data["truncation"].update(hard_cut=True), "must be marked truncated"),
        (lambda data: data["truncation"].update(stubbed_paths=[""]), "non-empty strings"),
        (lambda data: data["truncation"].update(stubbed_paths="oops"), "must be an array"),
    ],
)
def test_review_context_rejects_malformed_or_inconsistent_data(
    mutation: object, message: str
) -> None:
    data = ReviewContext.from_collected(_collected()).to_dict()
    assert callable(mutation)
    mutation(data)

    with pytest.raises(ArtifactError, match=message):
        ReviewContext.from_dict(data)


def test_diff_bytes_must_match_the_typed_context() -> None:
    context = ReviewContext.from_collected(_collected())

    with pytest.raises(ArtifactError, match="byte count"):
        context.to_collected("different\n")
