# grok-pr-review-action

Public MIT GitHub Action: [xAI Grok](https://docs.x.ai) pull request reviews with a **full-PR first pass** and a **latest-commit follow-up**.

Author: **Nathan (FlyOverCoderKY) / RetireGolden, LLC**.

This repo is a learning project and a long-term replacement for workflows that always embed `gh pr diff` — including on `synchronize` follow-ups. Callers could say “review only the latest commit” in `custom_instructions`, but they still paid for the full pull request in the prompt. This action makes the embedded diff an explicit input.

It is an independent implementation. It does **not** require SuperGrok, `grok login`, or a copied `~/.grok/auth.json`.

## Why `latest-commit` exists

| Caller | Typical event | `review_scope` | `effort` |
| --- | --- | --- | --- |
| First pass | `opened`, `reopened`, `ready_for_review` | `full-pr` | your repo default (`high` is common) |
| Follow-up | `synchronize` | `latest-commit` | `low` |

- `full-pr` (default) embeds `gh pr diff` for the whole pull request, capped by `max_diff_kb` (default 300). A truncated review receives a visible `partial` verdict and never appears clean.
- `latest-commit` embeds **only the new work on this push**. It prefers a linear `github.event.before...github.event.after` range. If `before` is missing, the comparison fails, or the history diverged after a force-push, it falls back to the **single latest commit on the reviewed PR head** and says so in the prompt. It never silently falls back to the full PR diff.

## Auth

Real auth is **`XAI_API_KEY` against `https://api.x.ai/v1` only**.

1. Create an API key at [console.x.ai](https://console.x.ai).
2. Store it as the repository secret `XAI_API_KEY`.
3. Pass it as an environment variable (not as an action input, so it stays out of input logs):

```yaml
env:
  XAI_API_KEY: ${{ secrets.XAI_API_KEY }}
```

The action writes `config.toml` under a job-specific temporary `GROK_HOME` that points the chosen model at `https://api.x.ai/v1` with `env_key = "XAI_API_KEY"`. It fails closed if the key is empty, never prints the key, and deletes only its own validated temporary directory in `always()`. It never reads, modifies, or deletes the runner user's `~/.grok` state.

The action downloads the pinned Grok CLI 1.0.5 binary for Linux x86-64 or arm64 and verifies its SHA-256 checksum before execution. Other operating systems fail with an explicit unsupported-runner error.

## Untrusted pull requests and fork PRs

Before Grok starts, the action copies the checkout into an inert temporary workspace. Repository agent instructions, MCP configuration, plugins, and symlinks are excluded. Grok then runs with the strict OS sandbox, no subagents or memory, and only `read_file`, `grep`, and `list_dir`. PR descriptions, source, and diffs are explicitly treated as untrusted data.

GitHub does not provide repository secrets or a write-capable token to ordinary `pull_request` workflows from public forks. Those runs therefore cannot use `XAI_API_KEY` or post a review. Do not work around that by blindly checking out and executing fork code under `pull_request_target`. Use a maintainer-gated workflow running trusted workflow/action code, or document that external fork PRs require a manual review dispatch.

## Copy-paste workflow

```yaml
name: Grok PR review

on:
  pull_request:
    types: [opened, reopened, ready_for_review, synchronize]

jobs:
  grok:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0
        with:
          persist-credentials: false
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}

      - name: First-pass review
        if: github.event.action != 'synchronize'
        uses: FlyOverCoderKY/grok-pr-review-action@v1
        with:
          github_token: ${{ github.token }}
          pr_number: ${{ github.event.pull_request.number }}
          model: grok-4.6
          effort: high
          review_scope: full-pr
        env:
          XAI_API_KEY: ${{ secrets.XAI_API_KEY }}

      - name: Follow-up review
        if: github.event.action == 'synchronize'
        uses: FlyOverCoderKY/grok-pr-review-action@v1
        with:
          github_token: ${{ github.token }}
          pr_number: ${{ github.event.pull_request.number }}
          model: grok-4.6
          effort: low
          review_scope: latest-commit
        env:
          XAI_API_KEY: ${{ secrets.XAI_API_KEY }}
```

Until a `v1` tag exists, pin a commit SHA or the branch you merged.

## Inputs

| Input | Default | Notes |
| --- | --- | --- |
| `github_token` | `${{ github.token }}` | Needs `pull-requests: write` to post the review. |
| `pr_number` | PR that triggered the workflow | Required for `workflow_dispatch`. |
| `model` | `grok-4.6` | Sent to the Grok CLI and written into `config.toml`. |
| `effort` | _empty_ | `low` \| `medium` \| `high` \| `xhigh`. |
| `max_turns` | `50` | Headless turn cap. Hitting it posts a visible incomplete comment. |
| `fail_on` | `never` | `never` never red-Xs the job just because issues were found. `bugs` / `any` fail on those findings. |
| `roast_level` | `professional` | `professional` \| `playful` \| `savage` \| `diabolical`. |
| `custom_instructions` | _empty_ | Extra prompt text (conventions, ignore rules). |
| `status_comments` | `true` | Live “Grokking this…” comment. |
| `max_diff_kb` | `300` | Embedded diff cap, with a truncation notice. |
| `review_scope` | `full-pr` | `full-pr` \| `latest-commit`. |

## Outputs

| Output | Meaning |
| --- | --- |
| `verdict` | `clean` \| `issues` \| `partial` \| `error` |
| `issue_count` | Structured findings |
| `bug_count` | Findings with `severity: bug` |
| `review_url` | Posted GitHub review, or the incomplete-review comment |

Grok runs headless with `--prompt-file` and JSON output. Tools are allowlisted to `read_file`, `grep`, and `list_dir`. There is no shell tool. `--yolo` only auto-approves those read-only tools.

If Grok exits unsuccessfully, reports a non-success stop reason, violates the output schema, hits `max_turns`, or returns no JSON, the action posts a visible PR comment that the review was incomplete and sets `verdict=error`. A GitHub posting failure is always an operational action failure. A completed review of a truncated or stale diff is posted with `verdict=partial` and a warning explaining why. Incomplete and partial coverage are never silent.

## Local tests

CI on this repo does not need a live `XAI_API_KEY`. It lints, typechecks, and unit-tests prompt building for both scopes:

```bash
python3 -m pip install -e '.[dev]'
ruff check src tests
ruff format --check src tests
mypy src
pytest
coverage run --source=src/grok_pr_review -m pytest
coverage report --fail-under=80
```

`tests/fixtures/full_pr.diff` and `tests/fixtures/latest_commit.diff` are the fixtures a reviewer can use to confirm `latest-commit` never embeds the full PR hunk.

## License

[MIT](LICENSE). Copyright (c) 2026 Fly Over Coder.
