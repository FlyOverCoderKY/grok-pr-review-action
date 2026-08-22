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

# Bubblewrap prep is tested with PATH stubs so unit/smoke does not need real apt
# or a kernel that allows user namespaces.
host_bash=$(command -v bash)
host_uname=$(command -v uname)
host_chmod=$(command -v chmod)
host_install=$(command -v install)
host_mkdir=$(command -v mkdir)
restrict_path() {
  local dir=$1
  mkdir -p "$dir"
  ln -s "$host_bash" "$dir/bash"
  ln -s "$host_uname" "$dir/uname"
  ln -s "$host_chmod" "$dir/chmod"
  ln -s "$host_install" "$dir/install"
  ln -s "$host_mkdir" "$dir/mkdir"
}

write_gated_bwrap() {
  local dir=$1
  cat > "$dir/bwrap" <<'BWRAP'
#!/usr/bin/env bash
if [[ -f "${BWRAP_USERNS_OK:?}" ]]; then
  exit 0
fi
echo "bwrap: setting up uid map: Permission denied" >&2
exit 1
BWRAP
  chmod +x "$dir/bwrap"
}

write_stub_sudo() {
  local dir=$1
  cat > "$dir/sudo" <<'SUDO'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SUDO_LOG:?}"
exec "$@"
SUDO
  chmod +x "$dir/sudo"
}

bwrap_present="$test_root/bwrap-present"
restrict_path "$bwrap_present"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$bwrap_present/bwrap"
printf '%s\n' '#!/usr/bin/env bash' 'echo unexpected sysctl >&2' 'exit 1' > "$bwrap_present/sysctl"
printf '%s\n' '#!/usr/bin/env bash' 'echo unexpected apparmor_parser >&2' 'exit 1' > "$bwrap_present/apparmor_parser"
chmod +x "$bwrap_present/bwrap" "$bwrap_present/sysctl" "$bwrap_present/apparmor_parser"
present_out=$(PATH="$bwrap_present" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap)
[[ "$present_out" == *"already available"* ]]
[[ "$present_out" == *"user namespaces already usable"* ]]

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

# AppArmor extra-profile path: copy bwrap-userns-restrict and reload it.
userns_apparmor="$test_root/userns-apparmor"
restrict_path "$userns_apparmor"
write_gated_bwrap "$userns_apparmor"
write_stub_sudo "$userns_apparmor"
mkdir -p "$test_root/extra-profiles" "$test_root/apparmor.d"
printf '%s\n' '# stub bwrap-userns-restrict' > "$test_root/extra-profiles/bwrap-userns-restrict"
export BWRAP_USERNS_OK="$test_root/userns-apparmor.ok"
export SUDO_LOG="$test_root/userns-apparmor.sudo"
export PARSER_LOG="$test_root/userns-apparmor.parser"
export GROK_APPARMOR_EXTRA_PROFILES_DIR="$test_root/extra-profiles"
export GROK_APPARMOR_D_DIR="$test_root/apparmor.d"
rm -f "$BWRAP_USERNS_OK" "$SUDO_LOG" "$PARSER_LOG"
cat > "$userns_apparmor/apparmor_parser" <<'PARSER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PARSER_LOG:?}"
: > "${BWRAP_USERNS_OK:?}"
exit 0
PARSER
printf '%s\n' '#!/usr/bin/env bash' 'echo unexpected sysctl >&2' 'exit 1' > "$userns_apparmor/sysctl"
chmod +x "$userns_apparmor/apparmor_parser" "$userns_apparmor/sysctl"
PATH="$userns_apparmor" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/userns-apparmor.out"
[[ -f "$BWRAP_USERNS_OK" ]]
[[ -f "$test_root/apparmor.d/bwrap-userns-restrict" ]]
grep -q 'bwrap-userns-restrict' "$test_root/userns-apparmor.out"
grep -q -- '-r' "$PARSER_LOG"
grep -q 'bwrap-userns-restrict' "$PARSER_LOG"
grep -q 'install -m 0644' "$SUDO_LOG"
grep -q 'apparmor_parser -r' "$SUDO_LOG"

# Missing extra profile: install apparmor-profiles, then load.
userns_apparmor_apt="$test_root/userns-apparmor-apt"
restrict_path "$userns_apparmor_apt"
write_gated_bwrap "$userns_apparmor_apt"
write_stub_sudo "$userns_apparmor_apt"
mkdir -p "$test_root/extra-profiles-apt" "$test_root/apparmor.d-apt"
export BWRAP_USERNS_OK="$test_root/userns-apparmor-apt.ok"
export SUDO_LOG="$test_root/userns-apparmor-apt.sudo"
export PARSER_LOG="$test_root/userns-apparmor-apt.parser"
export APT_LOG="$test_root/userns-apparmor-apt.apt"
export GROK_APPARMOR_EXTRA_PROFILES_DIR="$test_root/extra-profiles-apt"
export GROK_APPARMOR_D_DIR="$test_root/apparmor.d-apt"
rm -f "$BWRAP_USERNS_OK" "$SUDO_LOG" "$PARSER_LOG" "$APT_LOG"
rm -f "$GROK_APPARMOR_EXTRA_PROFILES_DIR/bwrap-userns-restrict"
cat > "$userns_apparmor_apt/apt-get" <<'APT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${APT_LOG:?}"
if [[ "${1:-}" == "install" ]]; then
  mkdir -p "${GROK_APPARMOR_EXTRA_PROFILES_DIR:?}"
  printf '%s\n' '# stub bwrap-userns-restrict from apt' \
    > "${GROK_APPARMOR_EXTRA_PROFILES_DIR}/bwrap-userns-restrict"
fi
exit 0
APT
cat > "$userns_apparmor_apt/apparmor_parser" <<'PARSER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PARSER_LOG:?}"
: > "${BWRAP_USERNS_OK:?}"
exit 0
PARSER
chmod +x "$userns_apparmor_apt/apt-get" "$userns_apparmor_apt/apparmor_parser"
PATH="$userns_apparmor_apt" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap
[[ -f "$BWRAP_USERNS_OK" ]]
grep -q 'install -y apparmor-profiles apparmor-utils' "$SUDO_LOG"
grep -q 'install -y apparmor-profiles apparmor-utils' "$APT_LOG"
grep -q 'apparmor_parser -r' "$SUDO_LOG"

# Dest already present (Ubuntu 25.04-style): reload without copying extras.
userns_apparmor_dest="$test_root/userns-apparmor-dest"
restrict_path "$userns_apparmor_dest"
write_gated_bwrap "$userns_apparmor_dest"
write_stub_sudo "$userns_apparmor_dest"
mkdir -p "$test_root/extra-profiles-empty" "$test_root/apparmor.d-dest"
printf '%s\n' '# already installed bwrap-userns-restrict' \
  > "$test_root/apparmor.d-dest/bwrap-userns-restrict"
export BWRAP_USERNS_OK="$test_root/userns-apparmor-dest.ok"
export SUDO_LOG="$test_root/userns-apparmor-dest.sudo"
export PARSER_LOG="$test_root/userns-apparmor-dest.parser"
export GROK_APPARMOR_EXTRA_PROFILES_DIR="$test_root/extra-profiles-empty"
export GROK_APPARMOR_D_DIR="$test_root/apparmor.d-dest"
rm -f "$BWRAP_USERNS_OK" "$SUDO_LOG" "$PARSER_LOG"
cat > "$userns_apparmor_dest/apparmor_parser" <<'PARSER'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PARSER_LOG:?}"
: > "${BWRAP_USERNS_OK:?}"
exit 0
PARSER
chmod +x "$userns_apparmor_dest/apparmor_parser"
PATH="$userns_apparmor_dest" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap
[[ -f "$BWRAP_USERNS_OK" ]]
grep -q 'apparmor_parser -r' "$SUDO_LOG"
if grep -q 'install -m 0644' "$SUDO_LOG"; then
  echo "reloaded dest profile should not copy a missing extra profile" >&2
  exit 1
fi

# Sysctl fallback when the AppArmor extra profile cannot be loaded.
userns_sysctl="$test_root/userns-sysctl"
restrict_path "$userns_sysctl"
write_gated_bwrap "$userns_sysctl"
write_stub_sudo "$userns_sysctl"
mkdir -p "$test_root/extra-profiles-sysctl" "$test_root/apparmor.d-sysctl"
export BWRAP_USERNS_OK="$test_root/userns-sysctl.ok"
export SUDO_LOG="$test_root/userns-sysctl.sudo"
export SYSCTL_LOG="$test_root/userns-sysctl.sysctl"
export GROK_APPARMOR_EXTRA_PROFILES_DIR="$test_root/extra-profiles-sysctl"
export GROK_APPARMOR_D_DIR="$test_root/apparmor.d-sysctl"
rm -f "$BWRAP_USERNS_OK" "$SUDO_LOG" "$SYSCTL_LOG"
cat > "$userns_sysctl/sysctl" <<'SYSCTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSCTL_LOG:?}"
if [[ "${1:-}" == "-n" ]]; then
  if [[ "${2:-}" == "kernel.unprivileged_userns_clone" ]]; then
    echo 0
    exit 0
  fi
  exit 1
fi
if [[ "${1:-}" == "-w" && "${2:-}" == "kernel.apparmor_restrict_unprivileged_userns=0" ]]; then
  : > "${BWRAP_USERNS_OK:?}"
  exit 0
fi
exit 0
SYSCTL
chmod +x "$userns_sysctl/sysctl"
PATH="$userns_sysctl" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/userns-sysctl.out"
[[ -f "$BWRAP_USERNS_OK" ]]
grep -q 'relaxing AppArmor unprivileged userns sysctls' "$test_root/userns-sysctl.out"
grep -qx 'sysctl -w kernel.apparmor_restrict_unprivileged_userns=0' "$SUDO_LOG"
grep -qx 'sysctl -w kernel.unprivileged_userns_clone=1' "$SUDO_LOG"
grep -q 'kernel.apparmor_restrict_unprivileged_userns=0' "$SYSCTL_LOG"
grep -q 'kernel.unprivileged_userns_clone=1' "$SYSCTL_LOG"

# Fail closed when uid maps still fail after remediations.
userns_fail="$test_root/userns-fail"
restrict_path "$userns_fail"
write_gated_bwrap "$userns_fail"
write_stub_sudo "$userns_fail"
mkdir -p "$test_root/extra-profiles-fail" "$test_root/apparmor.d-fail"
export BWRAP_USERNS_OK="$test_root/userns-fail.ok"
export SUDO_LOG="$test_root/userns-fail.sudo"
export SYSCTL_LOG="$test_root/userns-fail.sysctl"
export GROK_APPARMOR_EXTRA_PROFILES_DIR="$test_root/extra-profiles-fail"
export GROK_APPARMOR_D_DIR="$test_root/apparmor.d-fail"
rm -f "$BWRAP_USERNS_OK" "$SUDO_LOG" "$SYSCTL_LOG"
cat > "$userns_fail/sysctl" <<'SYSCTL'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${SYSCTL_LOG:?}"
exit 0
SYSCTL
chmod +x "$userns_fail/sysctl"
if PATH="$userns_fail" "$host_bash" "$repo_root/scripts/install-grok.sh" --ensure-bwrap \
  >"$test_root/userns-fail.out" 2>"$test_root/userns-fail.err"; then
  echo "ensure-bwrap succeeded when uid maps still failed" >&2
  exit 1
fi
[[ ! -f "$BWRAP_USERNS_OK" ]]
grep -q 'cannot set up a user namespace' "$test_root/userns-fail.err"
grep -q 'Refusing to start Grok with the sandbox unenforced' "$test_root/userns-fail.err"

unset BWRAP_USERNS_OK SUDO_LOG PARSER_LOG APT_LOG SYSCTL_LOG
unset GROK_APPARMOR_EXTRA_PROFILES_DIR GROK_APPARMOR_D_DIR

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
