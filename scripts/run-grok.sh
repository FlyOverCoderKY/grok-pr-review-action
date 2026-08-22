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

set +e
"$grok" --prompt-file "$prompt" \
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
