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
