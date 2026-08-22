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

# Test-only path overrides so install-script smoke can stub AppArmor
# without touching a real kernel. Production keeps the Ubuntu defaults.
apparmor_extra_profiles_dir=${GROK_APPARMOR_EXTRA_PROFILES_DIR:-/usr/share/apparmor/extra-profiles}
apparmor_d_dir=${GROK_APPARMOR_D_DIR:-/etc/apparmor.d}

resolve_cmd() {
  local name=$1
  local fallback=${2:-}
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi
  if [[ -n "$fallback" && -x "$fallback" ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  return 1
}

bwrap_userns_works() {
  # Minimal unprivileged userns probe. A uid-map failure is the Ubuntu 24.04
  # AppArmor symptom; do not require a full sandbox layout or a real kernel
  # in PATH-stub tests (those stubs ignore these flags).
  bwrap --unshare-user --die-with-parent --ro-bind / / --dev /dev --proc /proc --chdir / -- /bin/true >/dev/null 2>&1
}

have_apparmor_parser() {
  resolve_cmd apparmor_parser /usr/sbin/apparmor_parser >/dev/null
}

load_bwrap_apparmor_profile() {
  local src="${apparmor_extra_profiles_dir}/bwrap-userns-restrict"
  local dst="${apparmor_d_dir}/bwrap-userns-restrict"

  if [[ ! -f "$src" && ! -f "$dst" ]] || ! have_apparmor_parser; then
    if command -v apt-get >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
      echo "Installing apparmor-profiles and apparmor-utils so bwrap can use a userns AppArmor profile."
      if ! sudo apt-get update || ! sudo apt-get install -y apparmor-profiles apparmor-utils; then
        echo "apt-get could not install apparmor-profiles/apparmor-utils; will try sysctl fallback." >&2
      else
        hash -r
      fi
    fi
  fi

  if [[ ! -f "$src" && ! -f "$dst" ]]; then
    echo "AppArmor extra profile bwrap-userns-restrict is not available."
    return 1
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is not available; cannot load the bwrap AppArmor userns profile." >&2
    return 1
  fi

  if ! have_apparmor_parser; then
    echo "apparmor_parser is not available; cannot load the bwrap AppArmor userns profile." >&2
    return 1
  fi

  if [[ -f "$src" ]]; then
    echo "Loading AppArmor profile $src so unprivileged bwrap can create a user namespace."
    sudo install -m 0644 "$src" "$dst" || return 1
  else
    echo "Reloading existing AppArmor profile $dst so unprivileged bwrap can create a user namespace."
  fi
  sudo apparmor_parser -r "$dst"
}

relax_unprivileged_userns_sysctl() {
  local sysctl_bin
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is not available; cannot relax AppArmor unprivileged userns sysctls." >&2
    return 1
  fi
  if ! sysctl_bin=$(resolve_cmd sysctl /usr/sbin/sysctl); then
    echo "sysctl is not available; cannot relax AppArmor unprivileged userns sysctls." >&2
    return 1
  fi

  echo "Relaxing kernel.apparmor_restrict_unprivileged_userns so unprivileged bwrap can set up a uid map."
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || return 1
  if "$sysctl_bin" -n kernel.unprivileged_userns_clone >/dev/null 2>&1; then
    sudo sysctl -w kernel.unprivileged_userns_clone=1 || return 1
  fi
}

ensure_bwrap_userns() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    return 0
  fi

  if bwrap_userns_works; then
    echo "bubblewrap user namespaces already usable"
    return 0
  fi

  echo "bwrap cannot create a user namespace yet (Ubuntu 24.04+ AppArmor userns restriction)."

  if load_bwrap_apparmor_profile; then
    if bwrap_userns_works; then
      echo "Enabled bwrap user namespaces via AppArmor profile bwrap-userns-restrict."
      return 0
    fi
  fi

  if relax_unprivileged_userns_sysctl; then
    if bwrap_userns_works; then
      echo "Enabled bwrap user namespaces by relaxing AppArmor unprivileged userns sysctls for this job."
      return 0
    fi
  fi

  echo "error: bwrap cannot set up a user namespace (uid map Permission denied). Ubuntu 24.04+ sets kernel.apparmor_restrict_unprivileged_userns=1, so unprivileged bwrap needs the bwrap-userns-restrict AppArmor profile or a relaxed sysctl. Tried loading that profile and relaxing the sysctl. Self-hosted runners must allow one of those remediations. Refusing to start Grok with the sandbox unenforced." >&2
  return 1
}

ensure_bubblewrap() {
  if command -v bwrap >/dev/null 2>&1; then
    echo "bubblewrap already available: $(command -v bwrap)"
  else
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
  fi
  ensure_bwrap_userns
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
