# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic versioning for stable releases.

## Unreleased

### Added

- Document recommended caller concurrency: isolate first-pass (`full-pr` on `opened` / `reopened` / `ready_for_review` / `workflow_dispatch`) from synchronize follow-ups (`latest-commit`) so hot-push loops do not cancel the opening review or burn tokens on cancelled follow-ups. Optional merge gates should stay green only after first-pass landed. Concurrency and merge gating belong in the org reusable caller; this action still runs a single review.

### Fixed

- Install `bubblewrap` (`bwrap`) on Linux before invoking the Grok CLI so GitHub-hosted Ubuntu runners can enforce the strict sandbox deny list. If `bwrap` is already present the install is skipped; if it cannot be installed the action still fails closed. Self-hosted Linux runners must provide `bwrap` or allow `sudo apt-get install -y bubblewrap`. There is no production switch to disable the sandbox.
- Enable bubblewrap user namespaces on Ubuntu 24.04+ GitHub-hosted runners after `bwrap` is present. Ubuntu's `kernel.apparmor_restrict_unprivileged_userns=1` blocks unprivileged uid maps (`bwrap: setting up uid map: Permission denied`). The action loads `bwrap-userns-restrict` when available, otherwise relaxes the restriction for the job, then probes `bwrap --unshare-user`. There is still no production switch to disable `--sandbox strict`. Prep for v1.0.2.
- Copy the Grok prompt into the isolated review workspace (`$review_cwd/.grok-pr-review/prompt.md`) before invoking `--sandbox strict`. Bubblewrap only allows reading inside `--cwd`, so `$WORK/prompt.md` was denied on GitHub-hosted ubuntu-24.04 (`Permission denied (os error 13)`) after AppArmor userns setup succeeded. The action still uses `--sandbox strict`; it does not bind-mount the work tree or disable the sandbox. Prep for v1.0.3.

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
