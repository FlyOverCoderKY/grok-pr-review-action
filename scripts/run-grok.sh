#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: $0 <grok> <prompt> <cwd> <output> <stderr> <exit-file>" >&2
  exit 2
fi

grok=$1
prompt=$2
review_cwd=$3
output=$4
stderr_log=$5
exit_file=$6

extra_args=()
if [[ -n "${MODEL:-}" ]]; then
  extra_args+=(-m "$MODEL")
fi
if [[ -n "${EFFORT:-}" ]]; then
  extra_args+=(--effort "$EFFORT")
fi

# --sandbox strict / bwrap only allows reading inside --cwd. The action writes
# prompt.md next to the isolated workspace, so copy it under review_cwd first.
sandbox_prompt_dir="$review_cwd/.grok-pr-review"
sandbox_prompt="$sandbox_prompt_dir/prompt.md"
case "$sandbox_prompt" in
  "$review_cwd"/*) ;;
  *)
    echo "error: sandbox prompt path is not under the review cwd" >&2
    exit 1
    ;;
esac
mkdir -p -- "$sandbox_prompt_dir"
cp -- "$prompt" "$sandbox_prompt"
cleanup_sandbox_prompt() {
  rm -rf -- "$sandbox_prompt_dir"
}
trap cleanup_sandbox_prompt EXIT

set +e
"$grok" --prompt-file "$sandbox_prompt" \
  --output-format json \
  --yolo \
  --sandbox strict \
  --no-subagents \
  --no-memory \
  --disable-web-search \
  --tools "read_file,grep,list_dir" \
  --disallowed-tools "run_terminal_cmd,web_search,web_fetch,search_replace,Agent" \
  --max-turns "$MAX_TURNS" \
  --no-auto-update \
  --cwd "$review_cwd" \
  "${extra_args[@]}" \
  > "$output" \
  2> "$stderr_log"
exit_code=$?
set -e

printf '%s\n' "$exit_code" > "$exit_file"
echo "grok exit code: $exit_code"
echo "--- last stderr lines ---"
tail -n 30 "$stderr_log" || true
