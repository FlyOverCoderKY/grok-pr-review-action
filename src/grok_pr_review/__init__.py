"""Small library used by the grok-pr-review-action composite action."""

from grok_pr_review.auth import render_config_toml, require_xai_api_key
from grok_pr_review.prompt import build_prompt
from grok_pr_review.result import parse_grok_output, should_fail_job
from grok_pr_review.scope import plan_diff, truncate_diff

__all__ = [
    "build_prompt",
    "parse_grok_output",
    "plan_diff",
    "render_config_toml",
    "require_xai_api_key",
    "should_fail_job",
    "truncate_diff",
]
