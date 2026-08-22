#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
scratch=$(mktemp -d "${RUNNER_TEMP:-/tmp}/grok-pin-check.XXXXXX")
trap 'rm -rf -- "$scratch"' EXIT

for machine in x86_64 aarch64; do
  read -r version archive_arch checksum < <(
    bash "$repo_root/scripts/install-grok.sh" --print-pin "$machine"
  )
  binary="$scratch/grok-$archive_arch"
  curl --proto '=https' --tlsv1.2 --retry 3 -fsSL \
    "https://x.ai/cli/grok-${version}-linux-${archive_arch}" \
    -o "$binary"
  echo "$checksum  $binary" | sha256sum --check --status
  echo "Verified Grok CLI $version for $archive_arch."
done
