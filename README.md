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

## Data sent to xAI

This action uses the xAI API as an external processor. The prompt always sends PR metadata, the selected diff, and `custom_instructions` to xAI. When Grok uses `read_file`, `grep`, or `list_dir`, the relevant paths and repository content returned by those tools can also be sent to xAI as model context. Grok's response is then parsed locally and posted to GitHub as a PR review or issue comment.

Do not put secrets, credentials, regulated personal data, or unrelated confidential material in PR text, diffs, repository files, or `custom_instructions`. Obtain organizational approval before enabling the action for private or regulated repositories, and confirm that contributors have the rights and consents needed to send the content for external processing.

xAI's current [API security FAQ](https://docs.x.ai/developers/faq/security) says API requests and responses are retained for 30 days by default and are not used for training without explicit permission. xAI also documents optional Zero Data Retention and its limitations. This action does not enable or verify ZDR; configure and confirm the appropriate account policy with xAI before use. The applicable contractual terms are xAI's [Enterprise Terms](https://x.ai/legal/terms-of-service-enterprise) and, where personal data is involved, its [Data Processing Addendum](https://x.ai/legal/data-processing-addendum). Those policies can change, so review the current versions rather than relying only on this summary.

## Untrusted pull requests and fork PRs

Before Grok starts, the action materializes tracked files from the exact collected PR-head commit into an inert, size-bounded temporary workspace. Dirty files, untracked build output, repository agent instructions, MCP configuration, plugins, and symlinks are excluded. Grok then runs with the strict OS sandbox, no subagents or memory, and only `read_file`, `grep`, and `list_dir`. PR descriptions, source, and diffs are explicitly treated as untrusted data.

The checkout must contain the reviewed commit object (`fetch-depth: 0` is the safest setup). Git submodule contents are not expanded, and Git LFS files appear as the pointer blobs stored in Git rather than downloaded objects. Reviews that depend on expanded submodules or LFS assets need a separately designed, trusted materialization policy.

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
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
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
| `github_timeout_seconds` | `120` | Per-operation `gh` timeout; integer from 1 through 600. |
| `pr_number` | PR that triggered the workflow | Required for `workflow_dispatch`. |
| `model` | `grok-4.6` | Sent to the Grok CLI and written into `config.toml`. |
| `effort` | _empty_ | `low` \| `medium` \| `high` \| `xhigh`. |
| `max_turns` | `50` | Headless turn cap from 1 through 1000. Hitting it posts a visible incomplete comment and fails the job. |
| `fail_on` | `never` | Finding policy only: `never` does not fail for findings; `bugs` / `any` fail on those completed-review findings. Operational errors always fail. |
| `roast_level` | `professional` | Public-comment tone: `professional` \| `playful` \| `savage` \| `diabolical`. Prefer `professional` for contributor-facing repositories. |
| `allow_unprofessional_tone` | `false` | Governance opt-in required for public `savage` or `diabolical` comments. |
| `custom_instructions` | _empty_ | Extra prompt text (conventions, ignore rules), limited to 16,000 UTF-8 bytes. Never put secrets here. |
| `status_comments` | `true` | Live “Grokking this…” comment. |
| `max_diff_kb` | `300` | Embedded diff cap, with a truncation notice. |
| `review_scope` | `full-pr` | `full-pr` \| `latest-commit`. |
| `review_mode` | `auto` | Loop position: `auto` (opened = initial round, synchronize = verify round), or force `initial` \| `verify`. |
| `severity_schedule` | `nit,risk,bug` | Severity floor per round, comma-separated; the last entry repeats. Default: everything in round 1, bugs+risks in round 2, bugs only from round 3. |
| `verify_model` | _empty_ | Optional cheaper model for verify rounds. |
| `verify_effort` | _empty_ | Optional effort override for verify rounds. |
| `verify_escalation_lines` | `500` | A verify push changing more lines than this gets a full-severity review with the primary model. |

## Outputs

| Output | Meaning |
| --- | --- |
| `verdict` | `clean` \| `issues` \| `partial` \| `error` |
| `issue_count` | Open findings after this round (carried-over plus new) |
| `bug_count` | Open `severity: bug` findings after this round |
| `round` | Review-loop round number this run performed |
| `review_url` | Posted GitHub review, or the incomplete-review comment |

## The review loop

The action is built for an agentic PR loop: review → agent fixes → re-review, until clean.

**Round 1 (initial, on `opened`)** is deliberately exhaustive and recall-biased. The prompt tells Grok its findings are adjudicated by an automated fixing agent, not read by a human, so it should report every issue it can name a failure scenario for — bugs, risks, and nits — sweep the diff repeatedly until a sweep finds nothing new, and account for every file in a coverage manifest. Expect the finding count to be front-loaded: a thorough round 1 is cheaper than five shallow rounds.

**Rounds 2+ (verify, on `synchronize`)** are convergence rounds. The bot reads its own prior review, re-lists the open findings, and asks Grok to verdict each one — `fixed`, `not_fixed`, `fixed_incorrectly`, or `disputed` — using the fix commits and the fixing agent's comment-thread replies as evidence. A reasoned rebuttal settles a finding as disputed; it is never re-raised without new evidence. New findings are accepted only in code the fix commits touched, at or above the round's severity floor from `severity_schedule` — so by round 3 (default) a nit about comment phrasing is structurally unreportable. A verify push changing more than `verify_escalation_lines` lines escalates to a full-severity review automatically, because a massive mid-loop change means the loop is off the rails.

State lives in a hidden, base64-encoded ledger inside the bot's own review bodies — the fixing agent needs no bot-specific protocol, so this action coexists with other review bots. Dispositions come only from commits and ordinary GitHub comment replies. `issue_count`/`bug_count` outputs report **open** findings including unfixed carry-overs, so an agent loop should iterate until `bug_count` is `0` (or `issue_count`, if it chases risks too). Use `verify_model`/`verify_effort` to run verify rounds on a cheaper tier — verifying a fix is a much easier task than finding the issue was. Forcing `review_mode: initial` (or any `workflow_dispatch` run) resets the loop with a fresh exhaustive review.

Grok runs headless with `--prompt-file` and JSON output. Tools are allowlisted to `read_file`, `grep`, and `list_dir`. There is no shell tool. `--yolo` only auto-approves those read-only tools.

If Grok exits unsuccessfully, reports a non-success stop reason, violates the output schema, hits `max_turns`, returns no JSON, or loses its process-exit marker, the action reports `verdict=error` and fails the job regardless of `fail_on`. When GitHub is available, it posts a visible incomplete-review comment and retains any validated findings recovered before the failure. A GitHub posting failure is also an operational action failure and triggers a best-effort incomplete-review comment so the completed findings are not lost silently. A completed review of a truncated or stale diff is posted with `verdict=partial` and a warning explaining why. If diff collection or workspace preparation fails before Grok runs — for example because the PR head moved mid-collection — the failing step posts a best-effort incomplete comment naming the stage before the job fails. Incomplete and partial coverage are never silent.

Every GitHub body has a conservative UTF-8 budget. Large completed or incomplete reviews continue in follow-up comments at finding boundaries so validated findings are not silently omitted when inline comments cannot be submitted.

## Local tests

CI on this repo does not need a live `XAI_API_KEY`. Python 3.11 through 3.14 are supported and tested; the package deliberately excludes 3.15 until it is added to the matrix. CI also validates action metadata/workflows, checks shell scripts, exercises the extracted composite-action boundary with a stub Grok executable, and runs security linting:

```bash
python3 -m pip install -e '.[dev]'
ruff check src tests
ruff format --check src tests
mypy src
pytest
coverage run --source=src/grok_pr_review -m pytest
coverage report --fail-under=80
bash tests/integration/test_action_scripts.sh
```

`tests/fixtures/full_pr.diff` and `tests/fixtures/latest_commit.diff` are the fixtures a reviewer can use to confirm `latest-commit` never embeds the full PR hunk.

See [RELEASING.md](RELEASING.md) for immutable tag, checksum, major-alias, and dependency-update procedures, and [CHANGELOG.md](CHANGELOG.md) for compatibility changes.

## License

[MIT](LICENSE). Copyright (c) 2026 Fly Over Coder.
