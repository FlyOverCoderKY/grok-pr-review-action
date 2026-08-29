# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic versioning for stable releases.

## Unreleased

## [1.0.7] - 2026-08-29

### Added

- Generated-file triage for over-cap diffs: when a diff exceeds `max_diff_kb`, generated and vendored files (lock files, `node_modules/` / `vendor/` / `__generated__/` paths, minified assets, source maps, snapshots) and data files whose own diff exceeds 64 KB (`.json`, `.csv`, `.svg`, `.d.ts`, …) are embedded as header-only stubs before any positional cut, so hand-written source stays fully embedded. Stubbed files keep their `diff --git` header — they remain in the embedded diff's path set and the coverage contract — and each stub carries a visible note with the omitted hunk and byte counts. The review still posts `verdict=partial`, and the partial warning names the stubbed files. A diff that already fits the cap is never stubbed. Reference case: retiregolden.org#108's 1178 KB diff now embeds as 60 KB with every changed file present, instead of losing 6 of its 10 paths to the positional cut.

## [1.0.6] - 2026-08-28

### Fixed

- Dense PRs whose diff exceeds `max_diff_kb` no longer fail-close as `verdict=error` when Grok's coverage manifest does not line up with the truncated embed. Coverage entries for files outside the embedded diff — files Grok reviewed with its read-only tools — are now ignored instead of rejected as invalid structured output, and when the embed was truncated, coverage that misses embedded-diff files degrades the completed review to `verdict=partial` with a visible note instead of discarding it. An untruncated initial review still fails closed when coverage does not account for every embedded-diff file. The initial-round prompt now also tells Grok to list only embedded-diff files in `coverage`. This lets a required first-pass gate go green with an honest partial review on dense PRs (RetireGolden retiregolden.org#108: a 1178 KB diff truncated to 300 KB permanently redded the gate).

## [1.0.5] - 2026-08-25

### Fixed

- An initial review whose coverage manifest count for an embedded-diff file does not match the number of reported findings no longer fail-closes as `verdict=error`. Recovered findings are kept and the review posts a completed `issues` / `clean` / `partial` verdict with a visible count-mismatch note. Missing coverage, a missing JSON blob, a non-`EndTurn` stop, or a schema violation still fail closed. This unblocks org first-pass (`grok-org-first-pass:done`) when Grok finishes with `end_turn` but the tally is off by one (RetireGolden#305 7-vs-8 / 6-vs-7).

## [1.0.4] - 2026-08-24

### Changed

- Findings that cite files outside the embedded diff are no longer rejected as invalid structured output. Blast-radius and stale-doc nits (files not edited in the PR) are posted with the rest of the review. Findings on PR files omitted by `max_diff_kb` truncation still keep the review `partial`. Coverage remains required for every embedded-diff file, and per-file coverage counts must still match the findings kept for those files.

### Added

- Document recommended caller concurrency: isolate first-pass (`full-pr` on `opened` / `reopened` / `ready_for_review` / `workflow_dispatch`) from synchronize follow-ups (`latest-commit`) so hot-push loops do not cancel the opening review or burn tokens on cancelled follow-ups. Optional merge gates should stay green only after first-pass landed. Concurrency and merge gating belong in the org reusable caller; this action still runs a single review.

## [1.0.3] - 2026-08-22

### Fixed

- Copy the Grok prompt into the isolated review workspace (`$review_cwd/.grok-pr-review/prompt.md`) before invoking `--sandbox strict`. Bubblewrap only allows reading inside `--cwd`, so `$WORK/prompt.md` was denied on GitHub-hosted ubuntu-24.04 (`Permission denied (os error 13)`) after AppArmor userns setup succeeded. The action still uses `--sandbox strict`; it does not bind-mount the work tree or disable the sandbox.

## [1.0.2] - 2026-08-22

### Fixed

- Enable bubblewrap user namespaces on Ubuntu 24.04+ GitHub-hosted runners after `bwrap` is present. Ubuntu's `kernel.apparmor_restrict_unprivileged_userns=1` blocks unprivileged uid maps (`bwrap: setting up uid map: Permission denied`). The action loads `bwrap-userns-restrict` when available, otherwise relaxes the restriction for the job, then probes `bwrap --unshare-user`. There is still no production switch to disable `--sandbox strict`.

## [1.0.1] - 2026-08-22

### Fixed

- Install `bubblewrap` (`bwrap`) on Linux before invoking the Grok CLI so GitHub-hosted Ubuntu runners can enforce the strict sandbox deny list. If `bwrap` is already present the install is skipped; if it cannot be installed the action still fails closed. Self-hosted Linux runners must provide `bwrap` or allow `sudo apt-get install -y bubblewrap`. There is no production switch to disable the sandbox.

## [1.0.0] - 2026-08-22

First stable release. Immutable `v1.0.0` is tagged and smoke-tested before the movable `v1` alias follows (see [RELEASING.md](RELEASING.md)).

### Added

- Convergent **review loop** (PR #4 and follow-on ledger/coverage hardening) for the agentic review → fix → re-review cycle:
  - **Initial round** (round 1, typically on `opened` or `review_mode: initial`): exhaustive, recall-biased first pass with a required per-file **coverage manifest**. Coverage is enforced, not advisory: every embedded-diff file must be accounted for (including renames, mode-only changes, and binaries), per-file counts must match reported findings, and findings outside the embedded diff fail the job.
  - **Verify rounds** (rounds 2+, typically on `synchronize`): re-check open findings against the diff since the last published review and the fixing agent's comment-thread replies.
  - **`severity_schedule`** (default `nit,risk,bug`): severity floor per round; the last entry repeats. Default: all severities in round 1, bugs and risks in round 2, bugs only from round 3.
  - Hidden **bot ledger**: loop state is a base64 marker in the bot's own review bodies, bound to repository, PR, and reviewed commit. Only reviews authored by **`bot_login`** (default `github-actions[bot]`) are trusted.
  - Finding **dispositions**: `fixed`, `not_fixed`, `fixed_incorrectly`, `disputed`. A reasoned rebuttal settles a finding as disputed and it is not re-raised without new evidence.
  - Loop-aware outputs: **`round`**, plus **`issue_count`** and **`bug_count`** reporting **open** findings after the round (carried-over plus new).
  - **`review_mode`**: `auto` (opened = initial, synchronize = verify), or force `initial` / `verify`.
  - **`verify_model`**, **`verify_effort`**, and **`verify_escalation_lines`**: optional cheaper verify tier; a verify push over the line threshold (or a truncated verify diff) escalates to a full-severity review with the primary model.
  - **`bot_login`**: identity used to locate trusted ledger reviews. If `github_token` posts as a different login, this must match that identity or loop continuity is lost.
  - Fail-closed state recovery: forged, lost, corrupted, or stale ledger state does not silently reset; unresolved findings are never dropped from the ledger.

### Changed

- Operational review failures now fail the action independently of `fail_on`.
- Review artifacts are versioned and validated between composite-action steps.
- Grok reads an inert snapshot of the exact reviewed commit rather than mutable checkout state.
- GitHub operations are bounded by a configurable timeout and large review output is continued without dropping validated findings.
- Public `savage` and `diabolical` tones require an explicit governance opt-in.

### Security

- Missing or malformed Grok exit markers fail closed.
- Action inputs are validated before authentication, installation, or model execution.
