#!/usr/bin/env bash
set -euo pipefail

ACTION_VALIDATOR_VERSION="0.9.0"
ACTION_VALIDATOR_SHA256="9f42f94fca5b8d04c13bccfbb331104b37a9250650d89ae58dc888d46206f9b9"

if [[ "${1:-}" == "--print-pin" ]]; then
  [[ $# -eq 1 ]] || { echo "usage: $0 --print-pin" >&2; exit 2; }
  printf '%s %s\n' "$ACTION_VALIDATOR_VERSION" "$ACTION_VALIDATOR_SHA256"
  exit 0
fi

[[ $# -eq 1 ]] || { echo "usage: $0 <destination>" >&2; exit 2; }
destination=$1
if [[ "$(uname -s)" != "Linux" || "$(uname -m)" != "x86_64" ]]; then
  echo "The pinned action-validator installer supports Linux x86-64 only." >&2
  exit 1
fi

curl --proto '=https' --tlsv1.2 --retry 3 -fsSL \
  "https://github.com/mpalmer/action-validator/releases/download/v${ACTION_VALIDATOR_VERSION}/action-validator_linux_amd64" \
  -o "$destination"
echo "$ACTION_VALIDATOR_SHA256  $destination" | sha256sum --check --status
chmod 0755 "$destination"
