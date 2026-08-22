# Releasing

Stable releases use immutable `vMAJOR.MINOR.PATCH` tags. The movable `v1` alias is updated only after the immutable tag has passed CI and a smoke test in a disposable repository.

## Checklist

1. Review and date the `Unreleased` section in `CHANGELOG.md`.
2. Confirm the supported Python range and run the complete 3.11–3.14 CI matrix.
3. Run `bash scripts/verify-grok-pins.sh` to verify both published Grok binaries against the committed checksums.
4. Run Ruff, strict mypy, Bandit, unit/coverage tests, the checksum-pinned action-validator, digest-pinned actionlint, ShellCheck, and `tests/integration/test_action_scripts.sh`.
5. Create and push an immutable signed `vMAJOR.MINOR.PATCH` tag at the reviewed commit.
6. Test that immutable tag from a disposable PR using both `full-pr` and `latest-commit`.
7. Move the major alias (for example, `v1`) to the verified immutable tag and publish release notes from `CHANGELOG.md`.

Dependabot proposes pinned Python and GitHub Actions updates weekly. Keep version comments, immutable SHAs, Grok checksums, tests, and documentation in the same update PR.
