#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
test_root=$(mktemp -d "${RUNNER_TEMP:-/tmp}/grok-script-tests.XXXXXX")
trap 'rm -rf -- "$test_root"' EXIT

pin=$(bash "$repo_root/scripts/install-grok.sh" --print-pin x86_64)
[[ "$pin" == "1.0.5 x86_64 9ba87444e1819e8f6104adbbf4676a870c204380aa5c3e1c38a926c4ea677238" ]]
pin=$(bash "$repo_root/scripts/install-grok.sh" --print-pin aarch64)
[[ "$pin" == "1.0.5 aarch64 1c1fe67d7c35497fb09f44a451f57acc3787add4c9aea2c56f5c7c75dc5ffcf1" ]]
validator_pin=$(bash "$repo_root/scripts/install-action-validator.sh" --print-pin)
[[ "$validator_pin" == "0.9.0 9f42f94fca5b8d04c13bccfbb331104b37a9250650d89ae58dc888d46206f9b9" ]]

# Bubblewrap prep is tested with PATH stubs so unit/smoke does not need real apt.
host_bash=$(command -v bash)
host_uname=$(command -v uname)
host_chmod=$(command -v chmod)
restrict_path() {
  local dir=$1
  mkdir -p "$dir"
  ln -s "$host_bash" "$dir/bash"
  ln -s "$host_uname" "$dir/uname"
  ln -s "$host_chmod" "$dir/chmod"
}

bwrap_present="$test_root/bwrap-present"
restrict_path "$bwrap_present"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$bwrap_present/bwrap"
chmod +x "$bwrap_present/bwrap"
present_out=$(PATH="$bwrap_present" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap)
[[ "$present_out" == *"already available"* ]]

bwrap_none="$test_root/bwrap-none"
restrict_path "$bwrap_none"
if PATH="$bwrap_none" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/bwrap-none.out" 2>"$test_root/bwrap-none.err"; then
  echo "ensure-bwrap succeeded without apt-get or sudo" >&2
  exit 1
fi
grep -q 'apt-get is not available' "$test_root/bwrap-none.err"

bwrap_nosudo="$test_root/bwrap-nosudo"
restrict_path "$bwrap_nosudo"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$bwrap_nosudo/apt-get"
chmod +x "$bwrap_nosudo/apt-get"
if PATH="$bwrap_nosudo" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/bwrap-nosudo.out" 2>"$test_root/bwrap-nosudo.err"; then
  echo "ensure-bwrap succeeded without sudo" >&2
  exit 1
fi
grep -q 'sudo is not available' "$test_root/bwrap-nosudo.err"

bwrap_install="$test_root/bwrap-install"
restrict_path "$bwrap_install"
export SUDO_LOG="$test_root/sudo.log" APT_LOG="$test_root/apt.log" FAKE_BIN="$bwrap_install"
cat > "$bwrap_install/sudo" <<'SUDO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SUDO_LOG:?}"
exec "$@"
SUDO
cat > "$bwrap_install/apt-get" <<'APT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${APT_LOG:?}"
if [[ "${1:-}" == "install" ]]; then
  printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "${FAKE_BIN:?}/bwrap"
  chmod +x "${FAKE_BIN}/bwrap"
fi
exit 0
APT
chmod +x "$bwrap_install/sudo" "$bwrap_install/apt-get"
PATH="$bwrap_install" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap
[[ -x "$bwrap_install/bwrap" ]]
grep -qx 'apt-get update' "$SUDO_LOG"
grep -qx 'apt-get install -y bubblewrap' "$SUDO_LOG"
grep -q 'update' "$APT_LOG"
grep -q 'install -y bubblewrap' "$APT_LOG"

bwrap_missing_after="$test_root/bwrap-missing-after"
restrict_path "$bwrap_missing_after"
export SUDO_LOG="$test_root/sudo-missing.log" APT_LOG="$test_root/apt-missing.log"
cat > "$bwrap_missing_after/sudo" <<'SUDO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SUDO_LOG:?}"
exec "$@"
SUDO
cat > "$bwrap_missing_after/apt-get" <<'APT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${APT_LOG:?}"
exit 0
APT
chmod +x "$bwrap_missing_after/sudo" "$bwrap_missing_after/apt-get"
if PATH="$bwrap_missing_after" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/bwrap-missing-after.out" 2>"$test_root/bwrap-missing-after.err"; then
  echo "ensure-bwrap succeeded when apt-get did not provide bwrap" >&2
  exit 1
fi
grep -q 'still not on PATH' "$test_root/bwrap-missing-after.err"

stub="$test_root/grok"
args_file="$test_root/args"
cat > "$stub" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$STUB_ARGS_FILE"
printf '%s\n' '{"text":"{\"summary\":\"ok\",\"issues\":[]}","stopReason":"EndTurn"}'
echo "stub stderr" >&2
exit "${STUB_EXIT_CODE:-0}"
STUB
chmod +x "$stub"

mkdir -p "$test_root/workspace"
printf '%s\n' prompt > "$test_root/prompt.md"
export MODEL="grok-test" EFFORT="low" MAX_TURNS="12" STUB_ARGS_FILE="$args_file"
bash "$repo_root/scripts/run-grok.sh" \
  "$stub" "$test_root/prompt.md" "$test_root/workspace" \
  "$test_root/output.json" "$test_root/stderr.log" "$test_root/exit"
[[ "$(cat "$test_root/exit")" == "0" ]]
grep -q -- '--sandbox' "$args_file"
grep -q -- '--no-subagents' "$args_file"
grep -q -- "$test_root/workspace" "$args_file"
grep -q -- 'EndTurn' "$test_root/output.json"

STUB_EXIT_CODE=7 bash "$repo_root/scripts/run-grok.sh" \
  "$stub" "$test_root/prompt.md" "$test_root/workspace" \
  "$test_root/output-failed.json" "$test_root/stderr-failed.log" "$test_root/exit-failed"
[[ "$(cat "$test_root/exit-failed")" == "7" ]]

bad="$test_root/not-owned"
mkdir -p "$bad"
if bash "$repo_root/scripts/cleanup-workdir.sh" "$bad" "$test_root"; then
  echo "cleanup accepted an unsafe path" >&2
  exit 1
fi
[[ -d "$bad" ]]

owned="$test_root/grok-pr-review.ABC123"
mkdir -p "$owned"
bash "$repo_root/scripts/cleanup-workdir.sh" "$owned" "$test_root"
[[ ! -e "$owned" ]]

# An empty WORK (prepare step failed or run cancelled) is a no-op, not a failure.
bash "$repo_root/scripts/cleanup-workdir.sh" "" "$test_root"
