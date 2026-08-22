# Changelog

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic versioning for stable releases.

## Unreleased

## [1.0.0] - 2026-08-22

First stable release. Immutable `v1.0.0` is tagged after smoke; the movable `v1` alias follows (see [RELEASING.md](RELEASING.md)).

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
