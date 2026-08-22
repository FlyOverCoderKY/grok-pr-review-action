"""GitHub CLI helpers for collecting diffs and posting reviews."""

from __future__ import annotations

import json
import os

# Calls use fixed argv lists and never invoke a shell.
import subprocess  # nosec B404
from typing import Any

from grok_pr_review.result import (
    ReviewResult,
    extract_finding_marker,
    format_incomplete_comment_parts,
    format_review_body_parts,
    inline_review_comments,
    limit_github_body,
)
from grok_pr_review.scope import GhError

STATUS_MARKER = "<!-- grok-pr-review-action-status -->"


class GitHubCli:
    def __init__(
        self,
        repo: str,
        env: dict[str, str] | None = None,
        *,
        timeout_seconds: int = 120,
    ) -> None:
        self.repo = repo
        self.env = os.environ.copy() if env is None else dict(env)
        self.timeout_seconds = timeout_seconds

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
        return _json_object(raw, "gh pr view returned unexpected JSON")

    def pr_diff(self, number: int) -> str:
        return self._run(["pr", "diff", str(number), "--repo", self.repo])

    def compare_diff(self, before: str, after: str) -> str:
        endpoint = f"repos/{self.repo}/compare/{before}...{after}"
        comparison = _json_object(self._api([endpoint]), "could not parse commit comparison")
        status = comparison.get("status")
        behind_by = comparison.get("behind_by")
        if status != "ahead" or behind_by not in {0, None}:
            raise GhError("commit comparison is not a linear fast-forward range")
        return self._api(
            [
                "-H",
                "Accept: application/vnd.github.diff",
                endpoint,
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
        comments = self._paginated_list(
            f"repos/{self.repo}/issues/{pr_number}/comments", "issue comments"
        )
        for comment in reversed(comments):
            body = comment.get("body")
            ident = comment.get("id")
            if isinstance(body, str) and STATUS_MARKER in body and isinstance(ident, int):
                return ident
        return None

    def list_review_bodies(self, pr_number: int) -> list[str]:
        """Every review body on the PR, oldest first (the bot's own included)."""
        reviews = self._paginated_list(f"repos/{self.repo}/pulls/{pr_number}/reviews", "reviews")
        return [body for review in reviews if isinstance(body := review.get("body"), str)]

    def list_finding_replies(self, pr_number: int) -> list[tuple[str, str, str]]:
        """(finding_id, login, body) for replies to the bot's inline finding comments."""
        comments = self._paginated_list(
            f"repos/{self.repo}/pulls/{pr_number}/comments", "review comments"
        )
        finding_by_comment_id: dict[int, str] = {}
        for comment in comments:
            ident = comment.get("id")
            body = comment.get("body")
            if isinstance(ident, int) and isinstance(body, str):
                finding_id = extract_finding_marker(body)
                if finding_id:
                    finding_by_comment_id[ident] = finding_id
        replies: list[tuple[str, str, str]] = []
        for comment in comments:
            parent = comment.get("in_reply_to_id")
            body = comment.get("body")
            if not isinstance(parent, int) or not isinstance(body, str):
                continue
            finding_id = finding_by_comment_id.get(parent)
            if finding_id is None or extract_finding_marker(body):
                continue
            replies.append((finding_id, _comment_login(comment), body))
        return replies

    def list_recent_issue_comments(self, pr_number: int, limit: int = 30) -> list[tuple[str, str]]:
        """(login, body) for the newest PR conversation comments, oldest first."""
        comments = self._paginated_list(
            f"repos/{self.repo}/issues/{pr_number}/comments", "issue comments"
        )
        recent: list[tuple[str, str]] = []
        for comment in comments:
            body = comment.get("body")
            if not isinstance(body, str) or STATUS_MARKER in body:
                continue
            recent.append((_comment_login(comment), body))
        return recent[-limit:]

    def _paginated_list(self, endpoint: str, label: str) -> list[dict[str, Any]]:
        raw = self._api(["--paginate", "--slurp", endpoint])
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GhError(f"could not parse {label}") from exc
        if not isinstance(payload, list):
            return []
        items = (
            [item for page in payload for item in page]
            if payload and all(isinstance(page, list) for page in payload)
            else payload
        )
        return [item for item in items if isinstance(item, dict)]

    def upsert_status_comment(self, pr_number: int, body: str, comment_id: int | None) -> int:
        text = limit_github_body(f"{STATUS_MARKER}\n{body}")
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
            try:
                raw = self._api(
                    [
                        "--method",
                        "PATCH",
                        f"repos/{self.repo}/issues/comments/{comment_id}",
                        "-f",
                        f"body={text}",
                    ]
                )
            except GhError:
                raw = self._api(
                    [
                        "--method",
                        "POST",
                        f"repos/{self.repo}/issues/{pr_number}/comments",
                        "-f",
                        f"body={text}",
                    ]
                )
        data = _json_object(raw, "status comment response was invalid")
        ident = data.get("id")
        if not isinstance(ident, int):
            raise GhError("status comment response missing id")
        return ident

    def post_issue_comment(self, pr_number: int, body: str) -> str:
        body = limit_github_body(body)
        raw = self._api(
            [
                "--method",
                "POST",
                f"repos/{self.repo}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            ]
        )
        data = _json_object(raw, "issue comment response was invalid")
        url = data.get("html_url")
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
        hidden_marker: str | None = None,
        extra_lines: list[str] | None = None,
    ) -> str:
        bodies = format_review_body_parts(
            result,
            scope=scope,
            model=model,
            run_url=run_url,
            hidden_marker=hidden_marker,
            extra_lines=extra_lines,
        )
        body = bodies[0]
        comments = inline_review_comments(result)
        payload: dict[str, Any] = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
        }
        if comments:
            payload["comments"] = comments
        try:
            review_url = self._submit_review(pr_number, payload)
        except GhError:
            if "comments" not in payload:
                raise
            fallback = {
                "commit_id": commit_id,
                "body": body,
                "event": "COMMENT",
            }
            review_url = self._submit_review(pr_number, fallback)
        for continuation in bodies[1:]:
            self.post_issue_comment(pr_number, continuation)
        return review_url

    def post_incomplete(
        self,
        pr_number: int,
        result: ReviewResult,
        *,
        scope: str,
        model: str,
        run_url: str,
    ) -> str:
        bodies = format_incomplete_comment_parts(result, scope=scope, model=model, run_url=run_url)
        first_url = self.post_issue_comment(pr_number, bodies[0])
        for continuation in bodies[1:]:
            self.post_issue_comment(pr_number, continuation)
        return first_url

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
        data = _json_object(raw, "review response was invalid")
        url = data.get("html_url")
        return url if isinstance(url, str) else ""

    def _run(self, args: list[str]) -> str:
        return self._exec(["gh", *args])

    def _api(self, args: list[str], stdin: str | None = None) -> str:
        return self._exec(["gh", "api", *args], stdin=stdin)

    def _exec(self, argv: list[str], stdin: str | None = None) -> str:
        # argv is passed directly and is never interpreted by a shell.
        try:
            completed = subprocess.run(  # nosec B603
                argv,
                check=False,
                capture_output=True,
                text=True,
                env=self.env,
                input=stdin,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            operation = " ".join(argv[:2])
            raise GhError(f"{operation} timed out after {self.timeout_seconds} seconds") from exc
        except OSError as exc:
            operation = " ".join(argv[:2])
            raise GhError(f"could not start {operation}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "gh failed").strip()
            raise GhError(detail[-2000:])
        return completed.stdout


def _comment_login(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, dict):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return "unknown"


def _json_object(raw: str, message: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GhError(message) from exc
    if not isinstance(data, dict):
        raise GhError(message)
    return data
