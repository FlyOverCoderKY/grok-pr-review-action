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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema_version=99), "schema_version"),
        (lambda data: data["pr"].update(number=0), "positive integer"),
        (lambda data: data["pr"].update(headRefOid="short"), "full commit SHA"),
        (lambda data: data["plan"].update(to_sha="b" * 40), "must match"),
        (lambda data: data["truncation"].update(embedded_bytes=1), "preserve every byte"),
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
