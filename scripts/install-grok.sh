#!/usr/bin/env bash
set -euo pipefail

GROK_VERSION="1.0.5"

select_pin() {
  case "$1" in
    x86_64|amd64)
      printf '%s %s\n' "x86_64" \
        "9ba87444e1819e8f6104adbbf4676a870c204380aa5c3e1c38a926c4ea677238"
      ;;
    arm64|aarch64)
      printf '%s %s\n' "aarch64" \
        "1c1fe67d7c35497fb09f44a451f57acc3787add4c9aea2c56f5c7c75dc5ffcf1"
      ;;
    *)
      echo "Unsupported Linux architecture: $1" >&2
      return 1
      ;;
  esac
}

ensure_bubblewrap() {
  if command -v bwrap >/dev/null 2>&1; then
    echo "bubblewrap already available: $(command -v bwrap)"
    return 0
  fi

  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Grok's strict sandbox requires bubblewrap (bwrap) on Linux. This action currently supports Linux runners only." >&2
    return 1
  fi

  if ! command -v apt-get >/dev/null 2>&1; then
    echo "error: bwrap is missing and apt-get is not available. Install bubblewrap (provides bwrap) on this runner. Self-hosted Linux runners must provide bubblewrap. Refusing to start with denied paths unprotected." >&2
    return 1
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "error: bwrap is missing and sudo is not available; cannot apt-get install bubblewrap. Install bubblewrap on this self-hosted runner or grant passwordless sudo. Refusing to start with denied paths unprotected." >&2
    return 1
  fi

  echo "Installing bubblewrap so Grok can enforce its Linux sandbox deny list."
  if ! sudo apt-get update || ! sudo apt-get install -y bubblewrap; then
    echo "error: apt-get could not install bubblewrap. GitHub-hosted Ubuntu runners should allow this; self-hosted runners must provide bwrap or permit sudo apt-get. Refusing to start with denied paths unprotected." >&2
    return 1
  fi

  hash -r
  if ! command -v bwrap >/dev/null 2>&1; then
    echo "error: bubblewrap installation finished but bwrap is still not on PATH. Refusing to start with denied paths unprotected." >&2
    return 1
  fi
  echo "Installed bubblewrap: $(command -v bwrap)"
}

if [[ "${1:-}" == "--print-pin" ]]; then
  [[ $# -eq 2 ]] || { echo "usage: $0 --print-pin <architecture>" >&2; exit 2; }
  read -r arch checksum < <(select_pin "$2")
  printf '%s %s %s\n' "$GROK_VERSION" "$arch" "$checksum"
  exit 0
fi

if [[ "${1:-}" == "--ensure-bwrap" ]]; then
  [[ $# -eq 1 ]] || { echo "usage: $0 --ensure-bwrap" >&2; exit 2; }
  ensure_bubblewrap
  exit 0
fi

[[ $# -eq 1 ]] || { echo "usage: $0 <work-directory>" >&2; exit 2; }
work=$1
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This action currently supports Linux runners only." >&2
  exit 1
fi
ensure_bubblewrap

read -r grok_arch grok_sha256 < <(select_pin "$(uname -m)")
mkdir -p "$work/bin"
download="$work/bin/grok.download"
curl --proto '=https' --tlsv1.2 --retry 3 -fsSL \
  "https://x.ai/cli/grok-${GROK_VERSION}-linux-${grok_arch}" \
  -o "$download"
echo "$grok_sha256  $download" | sha256sum --check --status
mv "$download" "$work/bin/grok"
chmod 0755 "$work/bin/grok"
"$work/bin/grok" --version
