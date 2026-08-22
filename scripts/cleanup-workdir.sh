#!/usr/bin/env bash
set -euo pipefail

[[ $# -eq 2 ]] || { echo "usage: $0 <work-directory> <runner-temp>" >&2; exit 2; }
work=$1
runner_temp=$2

if [[ -z "$work" ]]; then
  echo "No work directory was created; nothing to clean."
  exit 0
fi

parent=$(dirname -- "$work")
base=$(basename -- "$work")

if [[ "$parent" != "$runner_temp" || ! "$base" =~ ^grok-pr-review\.[A-Za-z0-9]+$ ]]; then
  echo "Refusing to clean unexpected path: ${work:-<empty>}" >&2
  exit 1
fi

rm -rf -- "$work"
