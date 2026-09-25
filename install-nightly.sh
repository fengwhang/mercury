#!/bin/bash
# ============================================================================
# Mercury Nightly Installer — `mercury-nightly` to ~/.mercury-nightly
# ============================================================================
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install-nightly.sh | bash
#   curl -fsSL .../install-nightly.sh | bash -s -- v0.0.142   # pin a tag
#   bash install-nightly.sh [tag] [--skip-setup ...]          # from a checkout
#
# Resolves the latest nightly (newest non-draft prerelease) via the GitHub
# API, then execs install.sh with the nightly env. Extra args pass through.
# A pinned tag skips discovery (no API call). Separate home, command, and
# update track from stable — the two installs never touch each other.
# ============================================================================
set -euo pipefail

TAG=""
ARGS=()
for a in "$@"; do
    case "$a" in
        v[0-9]*.[0-9]*.[0-9]*) TAG="$a" ;;
        *) ARGS+=("$a") ;;
    esac
done

if [ -z "$TAG" ]; then
    TAG="$(curl -fsSL --connect-timeout 10 --max-time 30 \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/Fengwhang/mercury/releases?per_page=20" \
        | python3 -c "import json,sys
for r in json.load(sys.stdin):
    if isinstance(r, dict) and not r.get('draft') and r.get('prerelease'):
        print(r['tag_name']); break")" \
        || { echo "✗ could not resolve the latest nightly (API unreachable) — pass a tag explicitly" >&2; exit 1; }
    [ -n "$TAG" ] || { echo "✗ no nightly prerelease found" >&2; exit 1; }
    echo "→ latest nightly: $TAG"
fi

export MERCURY_HOME="${MERCURY_HOME:-$HOME/.mercury-nightly}"
export MERCURY_CMD="${MERCURY_CMD:-mercury-nightly}"
export MERCURY_CHANNEL="nightly"

_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"
if [ -f "$_SELF_DIR/install.sh" ]; then
    exec bash "$_SELF_DIR/install.sh" "$TAG" "${ARGS[@]}"
else
    exec bash -c 'curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | MERCURY_HOME="$0" MERCURY_CMD="$1" MERCURY_CHANNEL=nightly bash -s -- "$2" "${@:3}"' \
        "$MERCURY_HOME" "$MERCURY_CMD" "$TAG" "${ARGS[@]}"
fi
