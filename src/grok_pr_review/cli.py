"""Composite-action entry points. Invoked as `python3 -m grok_pr_review <cmd>`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from grok_pr_review.auth import require_xai_api_key, write_grok_config
from grok_pr_review.github import GitHubCli
from grok_pr_review.prompt import PromptContext, build_prompt
from grok_pr_review.result import (
    ReviewResult,
    mark_partial,
    neutralize_mentions,
    parse_grok_output,
    should_fail_job,
)
from grok_pr_review.scope import (
    DiffPlan,
    DiffRequest,
    GhError,
    Truncation,
    collect_review_material,
    normalize_sha,
    parse_scope,
)
from grok_pr_review.workspace import prepare_review_workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok-pr-review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-auth", help="Fail closed when XAI_API_KEY is empty")
    sub.add_parser("write-config", help="Write GROK_HOME/config.toml for api.x.ai")
    sub.add_parser("collect", help="Fetch PR metadata and the scoped diff")
    sub.add_parser("prepare-workspace", help="Create an inert read-only review workspace")
    sub.add_parser("prompt", help="Write prompt.md from collected artifacts")
    sub.add_parser("start-status", help="Post or update the live status comment")
    sub.add_parser("finish", help="Parse Grok output and post the review")
    args = parser.parse_args(argv)

    commands = {
        "check-auth": cmd_check_auth,
        "write-config": cmd_write_config,
        "collect": cmd_collect,
        "prepare-workspace": cmd_prepare_workspace,
        "prompt": cmd_prompt,
        "start-status": cmd_start_status,
        "finish": cmd_finish,
    }
    return commands[args.command]()


def cmd_check_auth() -> int:
    require_xai_api_key()
    print("XAI_API_KEY is set (value not printed).")
    return 0


def cmd_write_config() -> int:
    require_xai_api_key()
    grok_home = Path(_require_env("GROK_HOME"))
    model = os.environ.get("MODEL", "grok-4.6").strip() or "grok-4.6"
    path = write_grok_config(grok_home, model)
    print(f"Wrote {path} (api.x.ai, env_key=XAI_API_KEY).")
    return 0


def cmd_collect() -> int:
    work = _work_dir()
    pr_number = _pr_number()
    scope = parse_scope(os.environ.get("REVIEW_SCOPE", "full-pr"))
    max_diff_kb = _positive_int(os.environ.get("MAX_DIFF_KB", "300"), "MAX_DIFF_KB")
    before, after, head = _event_shas()
    github = _github()
    collected = collect_review_material(
        pr_number=pr_number,
        request=DiffRequest(
            scope=scope,
            before_sha=before,
            after_sha=after,
            head_sha=head,
        ),
        max_diff_kb=max_diff_kb,
        github=github,
    )
    (work / "pr.json").write_text(json.dumps(collected.pr, indent=2), encoding="utf-8")
    (work / "diff.patch").write_text(collected.diff, encoding="utf-8")
    (work / "scope.json").write_text(
        json.dumps(
            {
                "scope": collected.plan.scope,
                "kind": collected.plan.kind,
                "from_sha": collected.plan.from_sha,
                "to_sha": collected.plan.to_sha,
                "fallback_notice": collected.plan.fallback_notice,
                "truncated": collected.truncation.truncated,
                "truncation_notice": collected.truncation.notice,
                "original_bytes": collected.truncation.original_bytes,
                "embedded_bytes": collected.truncation.embedded_bytes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Collected {collected.plan.kind} diff "
        f"({collected.truncation.embedded_bytes} bytes embedded)."
    )
    if collected.plan.fallback_notice:
        print(collected.plan.fallback_notice)
    if collected.truncation.notice:
        print(collected.truncation.notice)
    return 0


def cmd_prompt() -> int:
    work = _work_dir()
    pr = json.loads((work / "pr.json").read_text(encoding="utf-8"))
    scope = json.loads((work / "scope.json").read_text(encoding="utf-8"))
    diff = (work / "diff.patch").read_text(encoding="utf-8")
    plan = DiffPlan(
        scope=scope["scope"],
        kind=scope["kind"],
        from_sha=scope.get("from_sha"),
        to_sha=scope.get("to_sha"),
        fallback_notice=scope.get("fallback_notice"),
    )
    truncation = Truncation(
        text=diff,
        truncated=bool(scope.get("truncated")),
        original_bytes=int(scope.get("original_bytes") or len(diff.encode())),
        embedded_bytes=int(scope.get("embedded_bytes") or len(diff.encode())),
        max_diff_kb=_positive_int(os.environ.get("MAX_DIFF_KB", "300"), "MAX_DIFF_KB"),
    )
    text = build_prompt(
        PromptContext(
            pr=pr,
            plan=plan,
            truncation=truncation,
            roast_level=os.environ.get("ROAST_LEVEL", "professional"),
            custom_instructions=os.environ.get("CUSTOM_INSTRUCTIONS", ""),
        )
    )
    (work / "prompt.md").write_text(text, encoding="utf-8")
    print(f"Wrote {work / 'prompt.md'} ({len(text.encode())} bytes).")
    return 0


def cmd_prepare_workspace() -> int:
    work = _work_dir()
    source = Path(_require_env("SOURCE_WORKSPACE"))
    destination = work / "workspace"
    preparation = prepare_review_workspace(source, destination)
    (work / "workspace.json").write_text(
        json.dumps(
            {
                "files_copied": preparation.files_copied,
                "excluded_paths": preparation.excluded_paths,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Prepared inert review workspace with {preparation.files_copied} files "
        f"({len(preparation.excluded_paths)} control or symlink paths excluded)."
    )
    return 0


def cmd_start_status() -> int:
    if not _truthy(os.environ.get("STATUS_COMMENTS", "true")):
        return 0
    work = _work_dir()
    pr_number = _pr_number()
    scope = os.environ.get("REVIEW_SCOPE", "full-pr")
    model = os.environ.get("MODEL", "grok-4.6")
    run_url = os.environ.get("RUN_URL", "")
    body = f"Grokking PR #{pr_number} (`{scope}`, model `{model}`)…\n" + (
        f"\nWorkflow run: {run_url}\n" if run_url else ""
    )
    github = _github()
    existing = github.find_status_comment(pr_number)
    comment_id = github.upsert_status_comment(pr_number, body, existing)
    (work / "status-comment-id").write_text(str(comment_id), encoding="utf-8")
    print(f"Status comment id {comment_id}")
    return 0


def cmd_finish() -> int:
    work = _work_dir()
    pr_number = _pr_number()
    fail_on = os.environ.get("FAIL_ON", "never")
    scope = os.environ.get("REVIEW_SCOPE", "full-pr")
    model = os.environ.get("MODEL", "grok-4.6")
    run_url = os.environ.get("RUN_URL", "")
    raw = _read_optional(work / "grok-output.json")
    exit_code = _read_exit(work / "grok-exit")
    result = parse_grok_output(raw, exit_code=exit_code)
    scope_data = _read_json_object(work / "scope.json")
    if bool(scope_data.get("truncated")):
        notice = scope_data.get("truncation_notice")
        reason = notice if isinstance(notice, str) and notice.strip() else "The diff was truncated."
        result = mark_partial(result, reason)

    github = _github()
    review_url = ""
    posting_failed = False
    try:
        if result.incomplete:
            review_url = github.post_incomplete(
                pr_number, result, scope=scope, model=model, run_url=run_url
            )
            print(result.incomplete_reason or "Review incomplete.")
        else:
            pr = _read_json_object(work / "pr.json")
            commit_id = normalize_sha(_maybe_str(scope_data.get("to_sha")))
            if commit_id is None:
                commit_id = normalize_sha(_maybe_str(pr.get("headRefOid")))
            if commit_id is None:
                raise GhError("reviewed commit SHA is missing")

            live_pr = github.pr_view(pr_number)
            live_head = normalize_sha(_maybe_str(live_pr.get("headRefOid")))
            if live_head is None:
                raise GhError("current PR head SHA is missing")
            if live_head != commit_id:
                result = mark_partial(
                    result,
                    "The PR head advanced after this diff was collected. "
                    f"This review is pinned to commit {commit_id[:12]}.",
                )
            review_url = github.post_review(
                pr_number,
                commit_id,
                result,
                scope=scope,
                model=model,
                run_url=run_url,
            )
    except GhError as exc:
        reason = f"Failed to post PR feedback: {exc}"
        print(reason)
        result = ReviewResult(
            verdict="error",
            summary=result.summary,
            issues=result.issues,
            incomplete_reason=reason,
            stop_reason=result.stop_reason,
        )
        posting_failed = True

    _update_status(github, work, pr_number, result, scope, review_url)
    _write_outputs(result, review_url)
    (work / "result.json").write_text(
        json.dumps(
            {
                "verdict": result.verdict,
                "issue_count": result.issue_count,
                "bug_count": result.bug_count,
                "review_url": review_url,
                "incomplete_reason": result.incomplete_reason,
                "partial_reason": result.partial_reason,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"verdict={result.verdict} issues={result.issue_count} bugs={result.bug_count}")
    if posting_failed:
        return 1
    if should_fail_job(fail_on, result):
        print(f"Failing the job because fail_on={fail_on}.")
        return 1
    return 0


def _update_status(
    github: GitHubCli,
    work: Path,
    pr_number: int,
    result: Any,
    scope: str,
    review_url: str,
) -> None:
    if not _truthy(os.environ.get("STATUS_COMMENTS", "true")):
        return
    ident = _read_optional(work / "status-comment-id").strip()
    comment_id = int(ident) if ident.isdigit() else github.find_status_comment(pr_number)
    summary = f"Grok review finished: `{result.verdict}` (scope `{scope}`)."
    if review_url:
        summary += f"\n\n{review_url}"
    if result.incomplete_reason:
        summary += f"\n\n{neutralize_mentions(result.incomplete_reason)}"
    if result.partial_reason:
        summary += f"\n\nPartial review: {neutralize_mentions(result.partial_reason)}"
    try:
        github.upsert_status_comment(pr_number, summary, comment_id)
    except GhError as exc:
        print(f"Could not update status comment: {exc}")


def _write_outputs(result: Any, review_url: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"verdict={result.verdict}\n")
        handle.write(f"issue_count={result.issue_count}\n")
        handle.write(f"bug_count={result.bug_count}\n")
        handle.write(f"review_url={review_url}\n")


def _github() -> GitHubCli:
    repo = _require_env("GITHUB_REPOSITORY")
    env = os.environ.copy()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return GitHubCli(repo, env=env)


def _event_shas() -> tuple[str | None, str | None, str | None]:
    event: dict[str, Any] = {}
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        loaded = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            event = loaded
    pull = event.get("pull_request")
    head = None
    if isinstance(pull, dict):
        inner = pull.get("head")
        if isinstance(inner, dict) and isinstance(inner.get("sha"), str):
            head = inner["sha"]
    before = os.environ.get("EVENT_BEFORE") or event.get("before")
    after = os.environ.get("EVENT_AFTER") or event.get("after")
    head = os.environ.get("HEAD_SHA") or head
    return _maybe_str(before), _maybe_str(after), _maybe_str(head)


def _pr_number() -> int:
    raw = os.environ.get("PR_NUMBER") or os.environ.get("INPUT_PR_NUMBER") or ""
    if raw.strip().isdigit():
        return int(raw.strip())
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        loaded = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            number = loaded.get("number")
            if isinstance(number, int):
                return number
            pull = loaded.get("pull_request")
            if isinstance(pull, dict) and isinstance(pull.get("number"), int):
                return int(pull["number"])
    work = os.environ.get("WORK")
    if work:
        pr_path = Path(work) / "pr.json"
        if pr_path.is_file():
            loaded = json.loads(pr_path.read_text(encoding="utf-8"))
            number = loaded.get("number") if isinstance(loaded, dict) else None
            if isinstance(number, int):
                return number
    raise SystemExit("No PR number. Trigger on pull_request or pass pr_number.")


def _work_dir() -> Path:
    path = Path(_require_env("WORK"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def _positive_int(raw: str, name: str) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise SystemExit(f"{name} must be a positive integer")
    return value


def _read_optional(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _read_exit(path: Path) -> int:
    raw = _read_optional(path).strip()
    if raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit()):
        return int(raw)
    return 0


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read {path.name}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit(f"Could not read {path.name}: expected a JSON object")
    return loaded


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _maybe_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
