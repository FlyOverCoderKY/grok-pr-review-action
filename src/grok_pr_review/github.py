"""GitHub CLI helpers for collecting diffs and posting reviews."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from grok_pr_review.result import (
    ReviewResult,
    format_incomplete_comment,
    format_review_body,
    inline_review_comments,
)
from grok_pr_review.scope import GhError

STATUS_MARKER = "<!-- grok-pr-review-action-status -->"


class GitHubCli:
    def __init__(self, repo: str, env: dict[str, str] | None = None) -> None:
        self.repo = repo
        self.env = os.environ.copy() if env is None else dict(env)

    def pr_view(self, number: int) -> dict[str, object]:
        raw = self._run(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "number,title,body,url,author,headRefOid,baseRefName,headRefName,additions,deletions,changedFiles",
            ]
        )
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise GhError("gh pr view returned unexpected JSON")
        return data

    def pr_diff(self, number: int) -> str:
        return self._run(["pr", "diff", str(number), "--repo", self.repo])

    def compare_diff(self, before: str, after: str) -> str:
        return self._api(
            [
                "-H",
                "Accept: application/vnd.github.diff",
                f"repos/{self.repo}/compare/{before}...{after}",
            ]
        )

    def commit_diff(self, sha: str) -> str:
        return self._api(
            [
                "-H",
                "Accept: application/vnd.github.diff",
                f"repos/{self.repo}/commits/{sha}",
            ]
        )

    def find_status_comment(self, pr_number: int) -> int | None:
        raw = self._api(["--paginate", f"repos/{self.repo}/issues/{pr_number}/comments"])
        try:
            comments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GhError("could not parse issue comments") from exc
        if not isinstance(comments, list):
            return None
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            ident = comment.get("id")
            if isinstance(body, str) and STATUS_MARKER in body and isinstance(ident, int):
                return ident
        return None

    def upsert_status_comment(self, pr_number: int, body: str, comment_id: int | None) -> int:
        text = f"{STATUS_MARKER}\n{body}"
        if comment_id is None:
            raw = self._api(
                [
                    "--method",
                    "POST",
                    f"repos/{self.repo}/issues/{pr_number}/comments",
                    "-f",
                    f"body={text}",
                ]
            )
        else:
            raw = self._api(
                [
                    "--method",
                    "PATCH",
                    f"repos/{self.repo}/issues/comments/{comment_id}",
                    "-f",
                    f"body={text}",
                ]
            )
        data = json.loads(raw)
        ident = data.get("id") if isinstance(data, dict) else None
        if not isinstance(ident, int):
            raise GhError("status comment response missing id")
        return ident

    def post_issue_comment(self, pr_number: int, body: str) -> str:
        raw = self._api(
            [
                "--method",
                "POST",
                f"repos/{self.repo}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            ]
        )
        data = json.loads(raw)
        url = data.get("html_url") if isinstance(data, dict) else None
        return url if isinstance(url, str) else ""

    def post_review(
        self,
        pr_number: int,
        commit_id: str,
        result: ReviewResult,
        *,
        scope: str,
        model: str,
        run_url: str,
    ) -> str:
        body = format_review_body(result, scope=scope, model=model, run_url=run_url)
        comments = inline_review_comments(result)
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
        }
        if comments:
            payload["comments"] = comments
        try:
            return self._submit_review(pr_number, payload)
        except GhError:
            if "comments" not in payload:
                raise
            fallback = {
                "commit_id": commit_id,
                "body": body,
                "event": "COMMENT",
            }
            return self._submit_review(pr_number, fallback)

    def post_incomplete(
        self,
        pr_number: int,
        result: ReviewResult,
        *,
        scope: str,
        model: str,
        run_url: str,
    ) -> str:
        body = format_incomplete_comment(result, scope=scope, model=model, run_url=run_url)
        return self.post_issue_comment(pr_number, body)

    def _submit_review(self, pr_number: int, payload: dict[str, Any]) -> str:
        raw = self._api(
            [
                "--method",
                "POST",
                f"repos/{self.repo}/pulls/{pr_number}/reviews",
                "--input",
                "-",
            ],
            stdin=json.dumps(payload),
        )
        data = json.loads(raw)
        url = data.get("html_url") if isinstance(data, dict) else None
        return url if isinstance(url, str) else ""

    def _run(self, args: list[str]) -> str:
        return self._exec(["gh", *args])

    def _api(self, args: list[str], stdin: str | None = None) -> str:
        return self._exec(["gh", "api", *args], stdin=stdin)

    def _exec(self, argv: list[str], stdin: str | None = None) -> str:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            input=stdin,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh failed").strip()
            raise GhError(detail[-2000:])
        return completed.stdout
