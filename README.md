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

Linux runners also need [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) so Grok can enforce its strict OS sandbox deny list. GitHub-hosted Ubuntu images do not currently ship `bwrap`. The action installs the `bubblewrap` package with `sudo apt-get update && sudo apt-get install -y bubblewrap` when `bwrap` is missing. Self-hosted Linux runners must either provide `bwrap` on `PATH` or allow that passwordless `apt-get` install. If `bwrap` cannot be installed, the action fails closed and does **not** disable the sandbox.

Ubuntu 24.04 and later (including GitHub-hosted `ubuntu-latest`) set `kernel.apparmor_restrict_unprivileged_userns=1`, so unprivileged `bwrap` cannot set up a uid map unless an AppArmor profile grants `userns`. The symptom is `bwrap: setting up uid map: Permission denied` ([Ubuntu bug #2144531](https://bugs.launchpad.net/ubuntu/+source/bubblewrap/+bug/2144531), [VS Code #316046](https://github.com/microsoft/vscode/issues/316046)). After `bwrap` is present, the action loads `/usr/share/apparmor/extra-profiles/bwrap-userns-restrict` into `/etc/apparmor.d/` when that extra profile is available (installing `apparmor-profiles` / `apparmor-utils` when needed) and runs `apparmor_parser -r`. If the profile cannot be loaded, it relaxes `kernel.apparmor_restrict_unprivileged_userns` for the job (`kernel.unprivileged_userns_clone=1` when that key exists). A short `bwrap --unshare-user` probe must succeed before Grok starts. The action still fails closed and does **not** disable `--sandbox strict`.

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

`@v1` is the intended consumer pin. Until the movable `v1` alias exists, pin the immutable `v1.0.0` tag (`FlyOverCoderKY/grok-pr-review-action@v1.0.0`) or a commit SHA. [RELEASING.md](RELEASING.md) tags immutable `v1.0.0` first, smoke-tests that tag, then moves `v1`.

The example above is a single job with two steps. Org reusable callers with agent hot-push loops should use the [Recommended caller concurrency](#recommended-caller-concurrency) recipe so a `synchronize` run cannot cancel the opening `full-pr` review.

## Recommended caller concurrency

This action runs **one review per invocation**. It does not implement workflow concurrency or merge gating. Those belong in the org reusable caller (for example RetireGolden/.github, and later pegma-dev/.github) that wraps this action.

A single PR-wide `concurrency` group with `cancel-in-progress: true` treats `opened` and `synchronize` as the same run. The first agent push then cancels the in-progress first pass — you lose the opening review and still pay for the cancelled tokens. A follow-up that starts before that first pass has posted also fails closed: `latest-commit` verify needs the prior ledger.

### Recipe

Use **separate first-pass and follow-up jobs**, or distinct **job-level / two-suffix concurrency groups**, so the two passes cannot cancel each other.

| Pass | Events | `review_scope` | Cancel-in-progress |
| --- | --- | --- | --- |
| First-pass | `opened`, `reopened`, `ready_for_review`, `workflow_dispatch` | `full-pr` | Never from `synchronize`. Rapid first-passes may cancel each other. |
| Follow-up | `synchronize` | `latest-commit` | OK among follow-ups only. Start only after first-pass has completed. |

Do **not** put first-pass and follow-up in one concurrency group that `synchronize` can cancel.

A two-suffix workflow-level group is enough when the reusable workflow only runs this review:

```yaml
# Same PR, two groups: synchronize must not cancel an in-progress first-pass.
concurrency:
  group: grok-review-${{ github.repository }}-${{ github.event.pull_request.number || inputs.pr_number }}-${{ github.event.action == 'synchronize' && 'follow-up' || 'first-pass' }}
  cancel-in-progress: true
```

Job-level groups are better when the workflow has other jobs you do not want cancelled with a superseded follow-up:

```yaml
jobs:
  grok-first-pass:
    if: >-
      github.event_name == 'workflow_dispatch' ||
      github.event.action == 'opened' ||
      github.event.action == 'reopened' ||
      github.event.action == 'ready_for_review'
    concurrency:
      group: grok-first-pass-${{ github.repository }}-${{ github.event.pull_request.number || inputs.pr_number }}
      cancel-in-progress: true
    steps:
      # checkout, then:
      - uses: FlyOverCoderKY/grok-pr-review-action@v1
        with:
          review_scope: full-pr
          # ...

  grok-follow-up:
    if: github.event.action == 'synchronize'
    concurrency:
      group: grok-follow-up-${{ github.repository }}-${{ github.event.pull_request.number || inputs.pr_number }}
      cancel-in-progress: true
    steps:
      # Wait until grok-first-pass (or its check) succeeded for this PR.
      # Do not use needs: grok-first-pass — that job is skipped on synchronize.
      - uses: FlyOverCoderKY/grok-pr-review-action@v1
        with:
          review_scope: latest-commit
          # ...
```

### Follow-up waits for first-pass

A `synchronize` job should not invoke this action until the first-pass job (or its check) has a successful conclusion for the PR. `latest-commit` verify without a published ledger fails closed instead of silently resetting to round 1.

### Optional merge gate

If branch protection needs a required check, publish a dedicated first-pass status (for example `grok-first-pass`) and require that. Keep it green only after first-pass has landed. Do not require the follow-up job: cancelled `synchronize` runs would fail the gate even when the opening review succeeded.

## Inputs

| Input | Default | Notes |
| --- | --- | --- |
| `github_token` | `${{ github.token }}` | Needs `pull-requests: write` to post the review. If this is a PAT or GitHub App token, you must also set `bot_login` to the identity it posts as. |
| `github_timeout_seconds` | `120` | Per-operation `gh` timeout; integer from 1 through 600. |
| `pr_number` | PR that triggered the workflow | Required for `workflow_dispatch`. |
| `model` | `grok-4.6` | Sent to the Grok CLI and written into `config.toml`. Keep `verify_model` on this same id unless you consciously want a cheaper verify tier (see cache note below). |
| `effort` | _empty_ | `low` \| `medium` \| `high` \| `xhigh`. |
| `max_turns` | `50` | Headless turn cap from 1 through 1000. Hitting it posts a visible incomplete comment and fails the job. |
| `fail_on` | `never` | Finding policy only: `never` does not fail for findings; `bugs` / `any` fail on those completed-review findings. Operational errors always fail. |
| `roast_level` | `professional` | Public-comment tone: `professional` \| `playful` \| `savage` \| `diabolical`. Prefer `professional` for contributor-facing repositories. |
| `allow_unprofessional_tone` | `false` | Governance opt-in required for public `savage` or `diabolical` comments. |
| `custom_instructions` | _empty_ | Extra prompt text (conventions, ignore rules), limited to 16,000 UTF-8 bytes. Never put secrets here. |
| `status_comments` | `true` | Live “Grokking this…” comment. |
| `max_diff_kb` | `300` | Embedded diff cap, with a truncation notice. |
| `review_scope` | `full-pr` | `full-pr` \| `latest-commit`. Initial rounds require `full-pr`; `latest-commit` is for verification rounds. |
| `review_mode` | `auto` | Loop position: `auto` (opened = initial round, synchronize = verify round), or force `initial` \| `verify`. |
| `severity_schedule` | `nit,risk,bug` | Severity floor per round, comma-separated; the last entry repeats. Default: everything in round 1, bugs+risks in round 2, bugs only from round 3. |
| `verify_model` | _empty_ | Optional model for verify rounds (defaults to `model`). A different id is an intentional xAI prompt-cache miss versus the initial pass. Callers MAY set a cheaper verify tier; keep both on the same model (for example `grok-4.6`) unless you want that cost trade. |
| `verify_effort` | _empty_ | Optional effort override for verify rounds (defaults to `effort`). |
| `verify_escalation_lines` | `500` | A verify push changing more lines than this (or one whose diff was truncated) gets a full-severity review with the primary model. |
| `bot_login` | `github-actions[bot]` | Login whose reviews are trusted as loop-ledger authors. Must match the identity `github_token` posts as, or the loop loses continuity. See below. |

### `bot_login` and ledger continuity

Loop-ledger state is **only trusted from reviews authored by `bot_login`** (default `github-actions[bot]`, the identity `github.token` posts as).

If `github_token` is a PAT or GitHub App token that posts as a **different** login, you **must** set `bot_login` to that identity. Otherwise the action cannot see its own prior ledgers: it treats previous rounds as missing, breaks disposition tracking, and typically fails the verify run closed on `synchronize` + `latest-commit` (or starts a disconnected initial pass if the event and scope allow it). Matching `bot_login` to the posting identity is required for loop continuity — not optional polish.

### `verify_model` and xAI prompt cache

`verify_model` and `verify_effort` stay available so callers can run verify rounds on a cheaper tier. Setting `verify_model` to a different id than `model` is an **intentional xAI prompt-cache miss** on verify rounds: a different model does not share cache with the initial pass, so you pay for a cold prompt. That flexibility is retained. Recommend keeping both on the same model (for example `grok-4.6`) unless you consciously want the cheaper-tier trade over cache reuse.

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

**Round 1 (initial, on `opened`)** is deliberately exhaustive and recall-biased. The prompt tells Grok its findings are adjudicated by an automated fixing agent, not read by a human, so it should report every issue it can name a failure scenario for — bugs, risks, and nits — sweep the diff repeatedly until a sweep finds nothing new, and account for every file in a coverage manifest. The manifest is enforced, not advisory: an initial review whose coverage does not account for every **embedded-diff** file — including renames, mode-only changes, and binary files — is rejected as invalid output and fails the job. A per-file coverage count that does not match the findings kept for that file is not a parse error: recovered findings are posted with a completed `issues` / `clean` / `partial` verdict and a visible mismatch note, so a completed `end_turn` first-pass is not discarded. Findings may also cite files outside the embedded diff — a PR file omitted by `max_diff_kb` truncation, or a blast-radius / stale-doc file the change did not edit. Those citations are posted with the rest of the review; they are not a parse error and do not set `verdict=error`. A truncated embed is still `partial`. Expect the finding count to be front-loaded: a thorough round 1 is cheaper than five shallow rounds.

**Rounds 2+ (verify, on `synchronize`)** are convergence rounds. Under `latest-commit` scope the verification diff spans everything **since the last published review** (from the SHA recorded in the ledger to the current head), not just this push's webhook range — so concurrent pushes completing out of order can never leave a commit range unreviewed. The bot reads its own prior review, re-lists the open findings, and asks Grok to verdict each one — `fixed`, `not_fixed`, `fixed_incorrectly`, or `disputed` — using the fix commits and the fixing agent's comment-thread replies as evidence. A reasoned rebuttal settles a finding as disputed; it is never re-raised without new evidence. New findings are accepted only in code the fix commits touched, at or above the round's severity floor from `severity_schedule` — so by round 3 (default) a nit about comment phrasing is structurally unreportable. A verify push changing more than `verify_escalation_lines` lines — or one whose diff exceeded `max_diff_kb` and was truncated — escalates to a full-severity review automatically, because a massive mid-loop change means the loop is off the rails. When the floor rises, still-open lower-severity findings from earlier rounds are retired without a disposition — by design: by round 3 an unfixed nit is noise, not signal — so "open findings" means unresolved findings at or above the current floor.

State lives in a hidden, base64-encoded ledger inside the bot's own review bodies — the fixing agent needs no bot-specific protocol, so this action coexists with other review bots. The ledger is **only trusted from reviews authored by `bot_login`** (default `github-actions[bot]`) and is bound to the repository, PR, and reviewed commit, so other reviewers cannot forge loop state. If `github_token` posts as a different login and `bot_login` is left at the default, prior rounds are invisible: continuity is lost and disposition tracking breaks (see [`bot_login` and ledger continuity](#bot_login-and-ledger-continuity)). State recovery fails closed: if prior reviews cannot be read, if the newest ledger marker is corrupted, or if a `latest-commit` synchronize run finds no prior state at all, the run fails visibly instead of silently resetting to round 1 and discarding carried findings — only an explicit `review_mode: initial` with `review_scope: full-pr` (or a state-free `full-pr` collection) starts fresh. The ledger preserves every open finding or fails the run: settled disputed findings may be trimmed to fit the marker's size limits, but unresolved findings are never silently dropped. Any partial run — including a stale, truncated, or history-fallback review — posts its findings and warning but never publishes ledger state; the previous complete marker remains the retry boundary. Dispositions come only from commits and ordinary GitHub comment replies. Coverage is validated against the embedded diff's paths; findings may cite files outside that embed and are still posted. A per-file coverage count mismatch is noted on the completed review and does not fail-close an `EndTurn` result. `issue_count`/`bug_count` outputs report **open** findings including unfixed carry-overs, so an agent loop should iterate until `bug_count` is `0` (or `issue_count`, if it chases risks too). Use `verify_model`/`verify_effort` to run verify rounds on a cheaper tier when you want that trade — verifying a fix is a much easier task than finding the issue was. Setting `verify_model` different from `model` is supported, but it is an intentional xAI prompt-cache miss on verify rounds (different model = no shared cache with the initial pass). Keep both on the same model (for example `grok-4.6`) unless you consciously want a cheaper verify tier over cache reuse. The posted review is labeled with the model that actually ran. Forcing `review_mode: initial` with `review_scope: full-pr` (or any `workflow_dispatch` run using that scope) resets the loop with a fresh exhaustive review. Caller concurrency belongs in the reusable workflow, not this action: isolate first-pass from `synchronize` so hot-push loops do not cancel the opening review. See [Recommended caller concurrency](#recommended-caller-concurrency).

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
