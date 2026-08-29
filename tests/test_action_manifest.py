from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_action_yml_uses_xai_api_key_not_supergrok() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    install_script = (ROOT / "scripts" / "install-grok.sh").read_text(encoding="utf-8")
    run_script = (ROOT / "scripts" / "run-grok.sh").read_text(encoding="utf-8")
    cleanup_script = (ROOT / "scripts" / "cleanup-workdir.sh").read_text(encoding="utf-8")
    assert "review_scope" in text
    assert "latest-commit" in text
    assert "full-pr" in text
    assert "XAI_API_KEY" in text
    assert "check-auth" in text
    assert "write-config" in text
    assert "grok_auth_json" not in text
    assert "GROK_AUTH_JSON" not in text
    assert "SuperGrok" not in text
    assert "grok login" not in text
    assert "always()" in text or "if: ${{ always() }}" in text
    assert "validate-inputs" in text
    assert text.index("Validate inputs") < text.index("Require XAI_API_KEY")
    assert "github_timeout_seconds" in text
    assert "allow_unprofessional_tone" in text
    assert "review_mode" in text
    assert "severity_schedule" in text
    assert "verify_model" in text
    assert "verify_escalation_lines" in text
    assert "bot_login" in text
    assert "model-override" in text
    assert "steps.finish.outputs.round" in text
    assert "prepare-workspace" in text
    assert "scripts/install-grok.sh" in text
    assert "scripts/run-grok.sh" in text
    assert "scripts/cleanup-workdir.sh" in text
    assert "--sandbox strict" in run_script
    assert "--sandbox off" not in run_script
    assert 'sandbox_prompt="$sandbox_prompt_dir/prompt.md"' in run_script
    assert ".grok-pr-review" in run_script
    assert 'cp -- "$prompt" "$sandbox_prompt"' in run_script
    assert '--prompt-file "$sandbox_prompt"' in run_script
    assert "--no-subagents" in run_script
    assert "--disable-web-search" in run_script
    assert 'GROK_VERSION="1.0.5"' in install_script
    assert "sha256sum --check" in install_script
    assert "ensure_bubblewrap" in install_script
    assert "ensure_bwrap_userns" in install_script
    assert "apt-get install -y bubblewrap" in install_script
    assert "apparmor-profiles" in install_script
    assert "bwrap-userns-restrict" in install_script
    assert "apparmor_parser -r" in install_script
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in install_script
    assert "kernel.unprivileged_userns_clone=1" in install_script
    assert "--unshare-user" in install_script
    assert "--ensure-bwrap" in install_script
    assert "--sandbox off" not in install_script
    assert 'rm -rf -- "$work"' in cleanup_script
    assert 'rm -f "$HOME/.grok/' not in text
    assert 'rm -rf "$HOME/.grok' not in text


def test_readme_explains_latest_commit_and_auth() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "latest-commit" in text
    assert "full-pr" in text
    assert "XAI_API_KEY" in text
    assert "synchronize" in text
    assert "RetireGolden" in text
    assert "SuperGrok" in text  # mentioned as something we do not require
    assert "grok_auth_json" not in text.lower()
    assert "Data sent to xAI" in text
    assert "requests and responses are retained for 30 days by default" in text
    assert "allow_unprofessional_tone" in text
    assert "Operational errors always fail" in text
    assert "## The review loop" in text
    assert "severity floor" in text
    assert "bot_login" in text
    assert "github-actions[bot]" in text
    assert "only trusted from reviews authored by" in text
    assert "prompt-cache miss" in text
    assert "uses: FlyOverCoderKY/grok-pr-review-action@v1" in text
    assert "@v1.0.0" in text
    assert "bubblewrap" in text
    assert "`bwrap`" in text
    assert "Self-hosted Linux runners" in text
    assert "does **not** disable the sandbox" in text
    assert "apparmor_restrict_unprivileged_userns" in text
    assert "bwrap-userns-restrict" in text
    assert "Ubuntu 24.04" in text
    assert "does **not** disable `--sandbox strict`" in text
    assert "## Recommended caller concurrency" in text
    assert "one review per invocation" in text
    assert "Never from `synchronize`" in text
    assert "Start only after first-pass has completed" in text
    assert "Do not require the follow-up job" in text
    assert "org reusable caller" in text
    stale = "cancel-in-progress: true` to avoid paying for superseded runs."
    assert stale not in text


def test_changelog_dates_1_0_0_and_documents_review_loop() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## Unreleased") < text.index("## [1.0.6] - 2026-08-28")
    assert text.index("## [1.0.6] - 2026-08-28") < text.index("## [1.0.5] - 2026-08-25")
    assert text.index("## [1.0.5] - 2026-08-25") < text.index("## [1.0.4] - 2026-08-24")
    assert text.index("## [1.0.4] - 2026-08-24") < text.index("## [1.0.3] - 2026-08-22")
    assert text.index("## [1.0.3] - 2026-08-22") < text.index("## [1.0.2] - 2026-08-22")
    assert text.index("## [1.0.2] - 2026-08-22") < text.index("## [1.0.1] - 2026-08-22")
    assert text.index("## [1.0.1] - 2026-08-22") < text.index("## [1.0.0] - 2026-08-22")
    assert "tagged and smoke-tested" in text
    assert "coverage manifest" in text
    assert "severity_schedule" in text
    assert "bot_login" in text
    assert "fixed_incorrectly" in text
    assert "verify_model" in text
    assert "review_mode" in text
    unreleased, rest = text.split("## [1.0.6]", 1)
    assert "###" not in unreleased.split("## Unreleased", 1)[1]
    v106 = rest.split("## [1.0.5]", 1)[0]
    assert "truncated embed" in v106
    assert "verdict=partial" in v106
    assert "retiregolden.org#108" in v106
    v105 = rest.split("## [1.0.5]", 1)[1].split("## [1.0.4]", 1)[0]
    assert "coverage manifest count" in v105
    assert "verdict=error" in v105
    assert "end_turn" in v105
    v104 = rest.split("## [1.0.4]", 1)[1].split("## [1.0.3]", 1)[0]
    assert "outside the embedded diff" in v104
    assert "recommended caller concurrency" in v104
    assert "org reusable caller" in v104
    patches = rest.split("## [1.0.0]", 1)[0]
    assert "bubblewrap" in patches
    assert "apparmor_restrict_unprivileged_userns" in patches
    assert "bwrap-userns-restrict" in patches
    assert ".grok-pr-review/prompt.md" in patches
    assert "Permission denied (os error 13)" in patches
