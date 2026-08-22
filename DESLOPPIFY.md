# Desloppify Backlog

Baseline verified during this scan: 61 tests passed with 1 platform-capability skip, total coverage was 86%, Ruff and strict mypy passed, and the latest `main` GitHub Actions run succeeded.

Resolution verified after implementation: 98 tests pass with 2 platform-capability skips, coverage is 87%, and Ruff, formatting, strict mypy, Bandit, action-validator, actionlint, ShellCheck, and the Linux action-script smoke harness all pass.

## Recommended Sequence

1. Fix operational failure semantics and the fail-open exit marker together.
2. Publish the external AI data-boundary documentation.
3. Centralize input validation and GitHub body budgets.
4. Add GitHub CLI timeouts and composite-action integration checks.
5. Introduce validated artifact serialization, then materialize the exact reviewed commit and split the finish orchestration.
6. Expand or narrow Python support, then address the nice-to-have release and API polish.

## Critical Issues

- [x] Make incomplete or failed reviews fail the check independently of finding policy
  - Where: `src/grok_pr_review/result.py`, `should_fail_job`; `src/grok_pr_review/cli.py`, `cmd_finish`
  - Why it matters: `fail_on=never` and `fail_on=bugs` currently return success for a Grok timeout, malformed output, missing structured findings, or other `verdict=error`. Branch protection can therefore show a green review check even though no usable review completed; only GitHub posting failures are forced to return nonzero.
  - Recommendation: Separate operational completion from the findings policy. Always return nonzero for `verdict=error`, reserve `fail_on` for completed `clean`/`issues`/`partial` results, and update the README and regression tests to make that contract explicit.
  - Timing: Safe to fix now; it is a small behavioral correction, but callers relying on green incomplete runs should be called out in release notes.

## Medium Cleanup Items

- [x] Replace loosely typed JSON artifact handoffs with validated domain serialization
  - Where: `src/grok_pr_review/cli.py`, `cmd_collect`, `cmd_prompt`, `cmd_finish`; `src/grok_pr_review/scope.py`, `DiffPlan` and `Truncation`
  - Why it matters: `pr.json` and `scope.json` are written as ad hoc dictionaries, reconstructed manually, and read with inconsistent validation (`json.loads` in `cmd_prompt` versus `_read_json_object` in `cmd_finish`). Adding or renaming a field can produce late `KeyError`, `TypeError`, or silent defaulting across command boundaries.
  - Recommendation: Give the persisted review context one versioned dataclass/schema with `to_dict`/`from_dict` validation, and use it in collect, prompt, finish, and tests.
  - Timing: Safe to fix now before additional scope modes or result fields expand the artifact contract.

- [x] Split the finish command into explicit parse, post, and finalize phases
  - Where: `src/grok_pr_review/cli.py`, `cmd_finish`, `_update_status`, `_write_outputs`
  - Why it matters: `cmd_finish` owns artifact parsing, partial-state transitions, live-head checks, posting, error conversion, status updates, output emission, and exit policy. The helper signatures fall back to `Any`, weakening strict typing at the most stateful boundary and making failure-path changes difficult to reason about.
  - Recommendation: Introduce a small typed finish context/outcome and extract pure state calculation from GitHub side effects and filesystem/output finalization. Type `_update_status` and `_write_outputs` with `ReviewResult` or the new outcome type.
  - Timing: Safe to fix after the operational-error exit policy is settled, so the extraction captures the intended semantics once.

- [x] Add bounded execution and actionable timeout errors for GitHub CLI calls
  - Where: `src/grok_pr_review/github.py`, `GitHubCli._exec`
  - Why it matters: Every `gh` subprocess waits indefinitely. A stuck credential helper, network call, or CLI process can consume the entire workflow until a caller-level job timeout, and the composite action itself cannot impose a step timeout.
  - Recommendation: Add a configurable conservative subprocess timeout, translate `subprocess.TimeoutExpired` into `GhError` with the operation name, and test both successful and timed-out calls.
  - Timing: Safe to fix now; choose a default that still accommodates large PR diff downloads.

- [x] Centralize and front-load all action input validation
  - Where: `action.yml` inputs and shell assembly; `src/grok_pr_review/cli.py`; `src/grok_pr_review/auth.py`; `src/grok_pr_review/prompt.py`
  - Why it matters: `review_scope`, `max_diff_kb`, and model names are validated in different commands; `fail_on` is not checked until after the review; `roast_level` silently falls back; `max_turns` and `effort` are left to the CLI; and `status_comments` accepts different values in YAML and Python. Invalid configuration can fail late, behave inconsistently, or spend model time before being rejected.
  - Recommendation: Add one early `validate-inputs` command with typed configuration, explicit enums/ranges, a bounded `custom_instructions` length, and a single boolean grammar used by both the manifest and Python commands.
  - Timing: Safe to fix now; document any newly rejected values as configuration errors.

- [x] Apply one GitHub text-budget policy to every posted body and preserve omitted findings
  - Where: `src/grok_pr_review/result.py`, `format_review_body`, `format_incomplete_comment`; `src/grok_pr_review/cli.py`, `_update_status`; `src/grok_pr_review/github.py`, `post_review`
  - Why it matters: Only completed review bodies are capped. Runtime-provided incomplete reasons and status summaries can still exceed GitHub limits, while a capped body plus a rejected inline-comment batch can omit later findings entirely even though the displayed issue count includes them.
  - Recommendation: Introduce a shared UTF-8 body-budget helper for reviews, issue comments, and status updates; build output at finding boundaries; and provide a durable way to publish or link the complete structured findings when inline submission falls back.
  - Timing: Safe to fix now, with boundary tests for multibyte text, incomplete errors, status updates, and inline-comment fallback.

- [x] Materialize the review snapshot from the reviewed commit, not arbitrary runner state
  - Where: `src/grok_pr_review/workspace.py`, `prepare_review_workspace`; `action.yml`, `Prepare isolated review workspace`
  - Why it matters: `os.walk` copies every non-control regular file under `${{ github.workspace }}` without verifying that the caller checked out the reviewed SHA. A missing, stale, merge-ref, or locally modified checkout gives Grok misleading nearby context; prior dependency/build steps also cause ignored `.venv`, cache, coverage, `node_modules`, binary, and build-output trees to be duplicated into temporary storage and exposed to the model.
  - Recommendation: Materialize tracked content for the exact collected/reviewed commit into the inert snapshot (with deliberate submodule/LFS handling), retain the explicit control-file and symlink exclusions, and add size/file-count limits plus diagnostics for skipped content.
  - Timing: Should follow the typed artifact work so the reviewed SHA is carried into snapshot preparation; test missing checkout, wrong ref, dirty files, submodules, Git LFS, executable files, and legitimate ignored context.

- [x] Treat a missing or malformed Grok exit marker as an operational error
  - Where: `src/grok_pr_review/cli.py`, `_read_exit`, `cmd_finish`
  - Why it matters: `_read_exit` maps a missing, empty, or nonnumeric `grok-exit` artifact to exit code zero. A valid-looking `EndTurn` output can therefore be accepted even when the shell handoff that records process completion was lost or corrupted.
  - Recommendation: Return an explicit parse result or raise a focused artifact error unless the marker contains a valid integer; add missing, empty, malformed, zero, and nonzero tests.
  - Timing: Safe to fix alongside the operational-error exit-policy item.

- [x] Exercise the composite action as an integration boundary in CI
  - Where: `action.yml`; `.github/workflows/self-test.yml`; `tests/test_action_manifest.py`; mocked pipeline tests in `tests/test_cli.py`
  - Why it matters: The current suite tests Python commands and searches the manifest for strings, but it never executes the Bash step assembly, environment wiring, cleanup guard, installer selection, or action outputs. Invalid YAML/Bash or a mismatched environment variable can pass all unit tests.
  - Recommendation: Add action metadata schema validation plus `actionlint`/`shellcheck`, then create a deterministic smoke harness that injects stub `grok` and `gh` executables (or extracts the shell orchestration into testable scripts) and runs the composite path without live credentials.
  - Timing: Safe to fix now; add injection only in the test harness so production cannot bypass the pinned binary or auth boundary.

- [x] Test every supported Python runtime or narrow the declared support range
  - Where: `pyproject.toml`, `requires-python`; `.github/workflows/self-test.yml`, Python matrix
  - Why it matters: The package declares all Python versions from 3.11 upward, while CI covers only 3.11 and 3.12. New interpreter releases can break the action despite being implicitly advertised as supported.
  - Recommendation: Add the current supported 3.x releases to the matrix and define a deliberate upper-bound/update policy, or explicitly narrow `requires-python` until newer versions are verified.
  - Timing: Safe to fix now; test the same interpreter version supplied by the current `ubuntu-latest` image.

- [x] Document the external AI data boundary and privacy expectations
  - Where: `README.md`, authentication/security sections; `src/grok_pr_review/prompt.py`; Grok read-only workspace tools
  - Why it matters: The documentation explains secrets and sandboxing but does not plainly state that PR metadata, the embedded diff, custom instructions, and nearby repository files opened by Grok are transmitted to xAI. Organizations may enable the action on private or regulated code without realizing the third-party processing scope.
  - Recommendation: Add a concise data-flow/privacy section describing exactly what can leave the runner, link the applicable xAI data-handling terms, warn against placing secrets in custom instructions, and recommend an organizational approval step for sensitive repositories.
  - Timing: Safe to fix now; verify policy claims against current authoritative xAI documentation before publishing them.

## Nice-to-Have Polish

- [x] Remove or adopt the unused collected-review prompt adapter
  - Where: `src/grok_pr_review/prompt.py`, `build_prompt_from_collected`
  - Why it matters: The helper has no production or test caller; `cmd_prompt` reconstructs `PromptContext` directly. Keeping two entry paths suggests an abstraction that the current artifact workflow does not actually use.
  - Recommendation: Delete it, or make the validated artifact/domain model call it as the single prompt-building entry point.
  - Timing: Safe to fix after deciding the typed artifact design.

- [x] Stop persisting an internal workspace manifest unless it has a consumer
  - Where: `src/grok_pr_review/cli.py`, `cmd_prepare_workspace`; `workspace.json`; `tests/test_cli.py`
  - Why it matters: `workspace.json` is written, tested, and immediately deleted during cleanup, but no later command or action output reads it. The file adds schema and test surface without currently supporting diagnostics or behavior.
  - Recommendation: Either expose its counts in action outputs/job summaries for real diagnostics or replace it with concise logging and remove the unused artifact.
  - Timing: Safe to fix now; retain it if the snapshot-size work will make the manifest user-visible.

- [x] Establish a repeatable dependency and action-release update workflow
  - Where: `action.yml` Grok version/checksums; `pyproject.toml` exact dev/build pins; `.github/workflows/self-test.yml` action SHAs; README `@v1` guidance
  - Why it matters: All important supply-chain inputs are pinned, which is good, but every update is currently manual and there is no release/tag yet. It is easy for the Grok version, two architecture checksums, comments, README, Python tools, and action SHAs to drift apart or remain stale.
  - Recommendation: Add Dependabot or equivalent for Actions/Python, a checksum-refresh verification script for Grok releases, a changelog/release checklist, immutable release tags, and a deliberately moved major `v1` alias.
  - Timing: Safe to prepare now; create the public major tag only after the intended v1 behavior and compatibility policy are finalized.

- [x] Make public-comment tone presets safer and easier to govern
  - Where: `action.yml`, `roast_level`; `src/grok_pr_review/prompt.py`, `_ROAST_GUIDANCE`; README input table
  - Why it matters: `savage` and `diabolical` explicitly encourage cutting public feedback. That can conflict with contributor codes of conduct, produce inconsistent review quality, and make organizations reluctant to adopt the action even when the technical findings are sound.
  - Recommendation: Keep `professional` as the documented default, add a warning that tone applies to public PR comments, and consider replacing personality labels with behavior-focused presets or allowing repositories to disable nonprofessional modes.
  - Timing: Safe to fix before the first stable release; changing preset names later would be a compatibility break.
