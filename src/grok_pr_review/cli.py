"""Composite-action entry points. Invoked as `python3 -m grok_pr_review <cmd>`."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from grok_pr_review.artifacts import ArtifactError, ReviewContext
from grok_pr_review.auth import AuthError, require_xai_api_key, write_grok_config
from grok_pr_review.config import (
    DEFAULT_BOT_LOGIN,
    DEFAULT_SEVERITY_SCHEDULE,
    DEFAULT_VERIFY_ESCALATION_LINES,
    MAX_DIFF_KB,
    MAX_GITHUB_TIMEOUT_SECONDS,
    MAX_VERIFY_ESCALATION_LINES,
    ActionConfig,
    ConfigError,
    Severity,
    parse_bool,
    parse_bot_login,
    parse_bounded_int,
    parse_custom_instructions,
    parse_effort,
    parse_fail_on,
    parse_model,
    parse_optional_model,
    parse_review_mode,
    parse_review_scope,
    parse_roast_level,
    parse_severity_schedule,
)
from grok_pr_review.github import GitHubCli
from grok_pr_review.loop import (
    Ledger,
    LoopState,
    RoundOutcome,
    apply_round,
    build_loop_state,
    count_changed_lines,
    decide_loop_state,
    encode_ledger,
    latest_ledger,
    render_agent_context,
)
from grok_pr_review.prompt import build_prompt_from_collected
from grok_pr_review.result import (
    ReviewResult,
    coverage_count_mismatches,
    format_pipeline_failure_comment,
    mark_partial,
    neutralize_mentions,
    note_coverage_count_mismatch,
    parse_grok_output,
    paths_outside_embed,
    should_fail_job,
    validate_coverage,
)
from grok_pr_review.scope import (
    CollectedReview,
    DiffRequest,
    GhError,
    changed_paths,
    collect_review_material,
    normalize_sha,
)
from grok_pr_review.workspace import WorkspaceError, prepare_review_workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grok-pr-review")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-inputs", help="Validate every composite-action input")
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
        "validate-inputs": cmd_validate_inputs,
        "write-config": cmd_write_config,
        "collect": cmd_collect,
        "prepare-workspace": cmd_prepare_workspace,
        "prompt": cmd_prompt,
        "start-status": cmd_start_status,
        "finish": cmd_finish,
    }
    try:
        return commands[args.command]()
    except (ArtifactError, AuthError, ConfigError, GhError, WorkspaceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_validate_inputs() -> int:
    config = ActionConfig.from_env(os.environ)
    print(
        "Validated action inputs "
        f"(pr={config.pr_number}, scope={config.review_scope}, "
        f"model={config.model}, max_turns={config.max_turns})."
    )
    return 0


def cmd_check_auth() -> int:
    require_xai_api_key()
    print("XAI_API_KEY is set (value not printed).")
    return 0


def cmd_write_config() -> int:
    require_xai_api_key()
    grok_home = Path(_require_env("GROK_HOME"))
    model = parse_model(os.environ.get("MODEL", "grok-4.6"))
    verify_model = parse_optional_model(os.environ.get("VERIFY_MODEL", ""))
    path = write_grok_config(grok_home, model, [verify_model] if verify_model else [])
    print(f"Wrote {path} (api.x.ai, env_key=XAI_API_KEY).")
    return 0


def cmd_collect() -> int:
    work = _work_dir()
    pr_number = _pr_number()
    scope = parse_review_scope(os.environ.get("REVIEW_SCOPE", "full-pr"))
    max_diff_kb = parse_bounded_int(
        os.environ.get("MAX_DIFF_KB", "300"),
        "max_diff_kb",
        minimum=1,
        maximum=MAX_DIFF_KB,
    )
    before, after, head = _event_shas()
    github = _github()
    try:
        ledger, mode, round_number, schedule, escalation_lines = _resolve_loop_position(
            github, pr_number, scope
        )
    except GhError as exc:
        _report_pipeline_failure("review-loop state recovery", exc)
        raise
    if mode == "verify" and scope == "latest-commit" and ledger is not None:
        # Continuity: verify everything since the last published review, not
        # just this push's webhook range, so a run racing an unfinished older
        # run can never skip the commits that run was reviewing.
        before = ledger.reviewed_sha or before
    try:
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
    except (GhError, ValueError) as exc:
        _report_pipeline_failure("diff collection", exc)
        raise
    state = _stage_loop_inputs(
        work,
        github,
        pr_number,
        collected,
        ledger=ledger,
        mode=mode,
        round_number=round_number,
        schedule=schedule,
        escalation_lines=escalation_lines,
    )
    (work / "diff.patch").write_text(collected.diff, encoding="utf-8")
    ReviewContext.from_collected(collected, loop=state).write(work / "review-context.json")
    print(
        f"Collected {collected.plan.kind} diff "
        f"({collected.truncation.embedded_bytes} bytes embedded)."
    )
    print(
        f"Review round {state.round_number} ({state.mode}, "
        f"severity floor {state.severity_floor}"
        f"{', escalated to a full-severity review' if state.escalated else ''})."
    )
    if collected.plan.fallback_notice:
        print(collected.plan.fallback_notice)
    if collected.truncation.notice:
        print(collected.truncation.notice)
    return 0


def _resolve_loop_position(
    github: GitHubCli, pr_number: int, scope: str
) -> tuple[Ledger | None, str, int, tuple[Severity, ...], int]:
    """Recover the loop position before collecting the diff.

    State recovery fails closed: a GitHub read failure or a corrupted newest
    ledger raises instead of silently resetting to round 1, because a reset
    during a latest-commit synchronize run would discard every carried
    finding. A state-free synchronize run is likewise refused under
    latest-commit scope, where an "initial" review of one push could report
    clean without ever seeing the rest of the PR. Only an explicit initial
    run (or a state-free full-pr collection) starts fresh.
    """
    mode_input = parse_review_mode(os.environ.get("REVIEW_MODE", "auto"))
    schedule = parse_severity_schedule(
        os.environ.get("SEVERITY_SCHEDULE", DEFAULT_SEVERITY_SCHEDULE)
    )
    escalation_lines = parse_bounded_int(
        os.environ.get("VERIFY_ESCALATION_LINES", str(DEFAULT_VERIFY_ESCALATION_LINES)),
        "verify_escalation_lines",
        minimum=1,
        maximum=MAX_VERIFY_ESCALATION_LINES,
    )
    repo = _require_env("GITHUB_REPOSITORY")
    bot_login = parse_bot_login(os.environ.get("BOT_LOGIN", DEFAULT_BOT_LOGIN))
    event_action = os.environ.get("EVENT_ACTION", "").strip().lower()

    ledger = None
    if mode_input != "initial":
        bodies = github.list_bot_review_bodies(pr_number, bot_login)
        ledger = latest_ledger(bodies, repo=repo, pr_number=pr_number)
        if ledger is None and mode_input == "verify":
            raise GhError(
                "review_mode is verify but no prior review-loop state exists on "
                "this PR; run an initial review first"
            )
        if ledger is None and event_action == "synchronize" and scope == "latest-commit":
            raise GhError(
                "this synchronize run collects only the latest commit and no "
                "prior review-loop state exists, so carried findings cannot be "
                "verified; run an initial full-PR review first "
                "(review_mode: initial, review_scope: full-pr)"
            )
    mode, round_number = decide_loop_state(
        review_mode=mode_input,
        event_action=event_action,
        ledger=ledger,
    )
    if mode == "initial" and scope != "full-pr":
        raise GhError(
            "an initial review must use review_scope: full-pr so the loop is "
            "seeded from the complete pull request"
        )
    return ledger, mode, round_number, schedule, escalation_lines


def _stage_loop_inputs(
    work: Path,
    github: GitHubCli,
    pr_number: int,
    collected: CollectedReview,
    *,
    ledger: Ledger | None,
    mode: str,
    round_number: int,
    schedule: tuple[Severity, ...],
    escalation_lines: int,
) -> LoopState:
    """Build the loop state from the collected diff and stage verify inputs."""
    escalated = mode == "verify" and (
        collected.truncation.truncated or count_changed_lines(collected.diff) > escalation_lines
    )
    state = build_loop_state(
        mode=mode,
        round_number=round_number,
        schedule=schedule,
        ledger=ledger,
        escalated=escalated,
    )

    replies_text = ""
    if state.mode == "verify":
        try:
            replies_text = render_agent_context(
                github.list_finding_replies(pr_number),
                github.list_recent_issue_comments(pr_number),
            )
        except GhError as exc:
            print(f"Could not fetch reviewer replies; verifying from commits only: {exc}")
    (work / "agent-replies.md").write_text(replies_text, encoding="utf-8")

    verify_model = parse_optional_model(os.environ.get("VERIFY_MODEL", ""))
    verify_effort = parse_effort(os.environ.get("VERIFY_EFFORT", ""))
    if state.mode == "verify" and not state.escalated:
        if verify_model:
            (work / "model-override").write_text(verify_model, encoding="utf-8")
        if verify_effort:
            (work / "effort-override").write_text(verify_effort, encoding="utf-8")
    return state


def cmd_prompt() -> int:
    work = _work_dir()
    context = ReviewContext.read(work / "review-context.json")
    diff = (work / "diff.patch").read_text(encoding="utf-8")
    allow_unprofessional = parse_bool(
        os.environ.get("ALLOW_UNPROFESSIONAL_TONE", "false"),
        "allow_unprofessional_tone",
    )
    text = build_prompt_from_collected(
        context.to_collected(diff),
        roast_level=parse_roast_level(
            os.environ.get("ROAST_LEVEL", "professional"),
            allow_unprofessional=allow_unprofessional,
        ),
        custom_instructions=parse_custom_instructions(os.environ.get("CUSTOM_INSTRUCTIONS", "")),
        allow_unprofessional_tone=allow_unprofessional,
        loop_state=context.loop,
        agent_replies=_read_optional(work / "agent-replies.md").strip(),
    )
    (work / "prompt.md").write_text(text, encoding="utf-8")
    print(f"Wrote {work / 'prompt.md'} ({len(text.encode())} bytes).")
    return 0


def cmd_prepare_workspace() -> int:
    work = _work_dir()
    try:
        context = ReviewContext.read(work / "review-context.json")
        source = Path(_require_env("SOURCE_WORKSPACE"))
        destination = work / "workspace"
        preparation = prepare_review_workspace(
            source,
            destination,
            reviewed_sha=context.plan.to_sha or "",
        )
    except (ArtifactError, WorkspaceError) as exc:
        _report_pipeline_failure("workspace preparation", exc)
        raise
    print(
        f"Prepared inert review workspace with {preparation.files_copied} files "
        f"({preparation.bytes_copied} bytes; "
        f"{len(preparation.excluded_paths)} control or non-regular paths excluded)."
    )
    return 0


def cmd_start_status() -> int:
    if not parse_bool(os.environ.get("STATUS_COMMENTS", "true"), "status_comments"):
        return 0
    work = _work_dir()
    pr_number = _pr_number()
    scope = parse_review_scope(os.environ.get("REVIEW_SCOPE", "full-pr"))
    model = parse_model(os.environ.get("MODEL", "grok-4.6"))
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


@dataclass(frozen=True)
class FinishOutcome:
    result: ReviewResult
    review_url: str


def cmd_finish() -> int:
    work = _work_dir()
    pr_number = _pr_number()
    fail_on = parse_fail_on(os.environ.get("FAIL_ON", "never"))
    scope = parse_review_scope(os.environ.get("REVIEW_SCOPE", "full-pr"))
    model = parse_model(os.environ.get("MODEL", "grok-4.6"))
    model_used = parse_optional_model(_read_optional(work / "model-override").strip()) or model
    run_url = os.environ.get("RUN_URL", "")
    status_comments = parse_bool(os.environ.get("STATUS_COMMENTS", "true"), "status_comments")
    context = ReviewContext.read(work / "review-context.json")
    result = _parse_finish_result(work, context)
    loop_outcome: RoundOutcome | None = None
    diff_paths: set[str] = set()
    if not result.incomplete:
        state = context.loop or build_loop_state(
            mode="initial",
            round_number=1,
            schedule=parse_severity_schedule(DEFAULT_SEVERITY_SCHEDULE),
            ledger=None,
            escalated=False,
        )
        diff = _read_optional(work / "diff.patch")
        diff_paths = changed_paths(diff)
        validation_error = (
            validate_coverage(result, diff_paths) if state.mode == "initial" else None
        )
        if validation_error:
            result = ReviewResult(
                verdict="error",
                summary=result.summary,
                issues=result.issues,
                incomplete_reason=(
                    f"Grok returned invalid structured findings: {validation_error}"
                ),
                stop_reason=result.stop_reason,
            )
        else:
            if state.mode == "initial":
                result = note_coverage_count_mismatch(
                    result, coverage_count_mismatches(result, diff_paths)
                )
            if context.truncated:
                omitted = paths_outside_embed(result, diff_paths)
                if omitted:
                    named = ", ".join(omitted[:5])
                    result = mark_partial(
                        result,
                        f"Findings cite file(s) omitted from the truncated embed: {named}",
                    )
            loop_outcome = apply_round(state, result)
            result = loop_outcome.result
    github = _github()
    outcome = _post_finish_result(
        github,
        pr_number,
        context,
        result,
        scope=scope,
        model=model_used,
        run_url=run_url,
        loop_outcome=loop_outcome,
        inline_paths=diff_paths,
    )
    return _finalize_finish(
        github,
        work,
        pr_number,
        outcome,
        scope=scope,
        fail_on=fail_on,
        status_comments=status_comments,
        loop_outcome=loop_outcome,
        round_number=context.loop.round_number if context.loop else 1,
    )


def _parse_finish_result(work: Path, context: ReviewContext) -> ReviewResult:
    raw = _read_optional(work / "grok-output.json")
    exit_code = _read_exit(work / "grok-exit")
    result = parse_grok_output(raw, exit_code=exit_code)
    if context.truncated:
        reason = context.truncation_notice or "The diff was truncated."
        result = mark_partial(result, reason)
    if (
        context.loop is not None
        and context.loop.mode == "verify"
        and context.plan.kind == "single-commit"
        and context.plan.fallback_notice
    ):
        result = mark_partial(
            result,
            "History diverged, so this verification round embedded only the "
            "single latest commit instead of the full range since the last review.",
        )
    return result


def _post_finish_result(
    github: GitHubCli,
    pr_number: int,
    context: ReviewContext,
    result: ReviewResult,
    *,
    scope: str,
    model: str,
    run_url: str,
    loop_outcome: RoundOutcome | None = None,
    inline_paths: set[str] | None = None,
) -> FinishOutcome:
    review_url = ""
    try:
        if result.incomplete:
            review_url = github.post_incomplete(
                pr_number, result, scope=scope, model=model, run_url=run_url
            )
            print(result.incomplete_reason or "Review incomplete.")
        else:
            commit_id = normalize_sha(context.plan.to_sha)
            if commit_id is None:
                raise GhError("reviewed commit SHA is missing")

            live_pr = github.pr_view(pr_number)
            live_head = normalize_sha(_maybe_str(live_pr.get("headRefOid")))
            if live_head is None:
                raise GhError("current PR head SHA is missing")
            stale = live_head != commit_id
            if stale:
                result = mark_partial(
                    result,
                    "The PR head advanced after this diff was collected. "
                    f"This review is pinned to commit {commit_id[:12]}.",
                )
            # A stale or partial run never publishes authoritative loop state.
            # The previous complete marker remains the retry boundary, so a
            # truncated or fallback diff cannot permanently skip unseen code.
            hidden_marker = None
            if loop_outcome is not None and not stale and result.verdict != "partial":
                hidden_marker = encode_ledger(
                    replace(loop_outcome.ledger, reviewed_sha=commit_id),
                    repo=_require_env("GITHUB_REPOSITORY"),
                    pr_number=pr_number,
                )
            review_url = github.post_review(
                pr_number,
                commit_id,
                result,
                scope=scope,
                model=model,
                run_url=run_url,
                hidden_marker=hidden_marker,
                extra_lines=loop_outcome.extra_lines if loop_outcome else None,
                inline_paths=inline_paths,
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
            partial_reason=result.partial_reason,
        )
        try:
            review_url = github.post_incomplete(
                pr_number, result, scope=scope, model=model, run_url=run_url
            )
        except GhError as post_exc:
            print(f"Could not post the incomplete-review comment: {post_exc}")
    return FinishOutcome(result=result, review_url=review_url)


def _finalize_finish(
    github: GitHubCli,
    work: Path,
    pr_number: int,
    outcome: FinishOutcome,
    *,
    scope: str,
    fail_on: str,
    status_comments: bool,
    loop_outcome: RoundOutcome | None = None,
    round_number: int = 1,
) -> int:
    result = outcome.result
    if loop_outcome is not None and not result.incomplete:
        counting_open = True
        issue_count = loop_outcome.open_issue_count
        bug_count = loop_outcome.open_bug_count
    else:
        counting_open = False
        issue_count = result.issue_count
        bug_count = result.bug_count
    _update_status(
        github,
        work,
        pr_number,
        result,
        scope,
        outcome.review_url,
        enabled=status_comments,
    )
    _write_outputs(
        result,
        outcome.review_url,
        issue_count=issue_count,
        bug_count=bug_count,
        round_number=round_number,
    )
    (work / "result.json").write_text(
        json.dumps(
            {
                "verdict": result.verdict,
                "issue_count": issue_count,
                "bug_count": bug_count,
                "round": round_number,
                "review_url": outcome.review_url,
                "incomplete_reason": result.incomplete_reason,
                "partial_reason": result.partial_reason,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"verdict={result.verdict} issues={issue_count} bugs={bug_count} round={round_number}")
    if should_fail_job(
        fail_on,
        result,
        open_bug_count=bug_count if counting_open else None,
        open_issue_count=issue_count if counting_open else None,
    ):
        reason = "the review did not complete" if result.incomplete else f"fail_on={fail_on}"
        print(f"Failing the job because {reason}.")
        return 1
    return 0


def _update_status(
    github: GitHubCli,
    work: Path,
    pr_number: int,
    result: ReviewResult,
    scope: str,
    review_url: str,
    *,
    enabled: bool,
) -> None:
    if not enabled:
        return
    ident = _read_optional(work / "status-comment-id").strip()
    summary = f"Grok review finished: `{result.verdict}` (scope `{scope}`)."
    if review_url:
        summary += f"\n\n{review_url}"
    if result.incomplete_reason:
        summary += f"\n\n{neutralize_mentions(result.incomplete_reason)}"
    if result.partial_reason:
        summary += f"\n\nPartial review: {neutralize_mentions(result.partial_reason)}"
    try:
        comment_id = int(ident) if ident.isdigit() else github.find_status_comment(pr_number)
        github.upsert_status_comment(pr_number, summary, comment_id)
    except GhError as exc:
        print(f"Could not update status comment: {exc}")


def _write_outputs(
    result: ReviewResult,
    review_url: str,
    *,
    issue_count: int,
    bug_count: int,
    round_number: int,
) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"verdict={result.verdict}\n")
        handle.write(f"issue_count={issue_count}\n")
        handle.write(f"bug_count={bug_count}\n")
        handle.write(f"round={round_number}\n")
        handle.write(f"review_url={review_url}\n")


def _report_pipeline_failure(stage: str, exc: Exception) -> None:
    """Best-effort visible PR comment when a step fails before finish can run."""
    try:
        github = _github()
        github.post_issue_comment(
            _pr_number(),
            format_pipeline_failure_comment(
                stage=stage,
                reason=str(exc),
                run_url=os.environ.get("RUN_URL", ""),
            ),
        )
    except (GhError, SystemExit) as post_exc:
        print(f"Could not post the pipeline-failure comment: {post_exc}", file=sys.stderr)


def _github() -> GitHubCli:
    repo = _require_env("GITHUB_REPOSITORY")
    env = os.environ.copy()
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    timeout_seconds = parse_bounded_int(
        os.environ.get("GITHUB_TIMEOUT_SECONDS", "120"),
        "github_timeout_seconds",
        minimum=1,
        maximum=MAX_GITHUB_TIMEOUT_SECONDS,
    )
    return GitHubCli(repo, env=env, timeout_seconds=timeout_seconds)


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
        context_path = Path(work) / "review-context.json"
        if context_path.is_file():
            number = ReviewContext.read(context_path).pr.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
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


def _read_optional(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _read_exit(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ArtifactError(f"could not read {path.name}: {exc}") from exc
    if not raw.isdigit():
        raise ArtifactError(f"{path.name} must contain a numeric process exit code")
    value = int(raw)
    if value > 255:
        raise ArtifactError(f"{path.name} contains an invalid process exit code: {value}")
    return value


def _maybe_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None
