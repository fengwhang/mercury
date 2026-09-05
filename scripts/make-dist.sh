#!/usr/bin/env bash
# make-dist.sh — assemble self-contained Mercury distribution tarballs.
# Output: PER-ARCH tarballs (user directive 2026-09-05: one binary per
# download, no dead weight):
#   dist/mercury-<version>-x64.tar.gz     (omp x86-64 prebuilt as dist/omp)
#   dist/mercury-<version>-arm64.tar.gz   (omp aarch64 prebuilt as dist/omp)
# Each contains the repo source (minus dev cruft) PLUS exactly ONE omp
# binary + ui-tui bundle so a clean VM needs neither bun nor rust nor
# esbuild. The release host cross-compiles the arm64 binary with
# CROSS_TARGET=linux-arm64 (natives embedded from the upstream
# @oh-my-pi/pi-natives-linux-arm64 version-matched prebuild).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VERSION="${MERCURY_VERSION:-$(sed -n "s/^__version__ = \"\(.*\)\".*/\1/p" hermes/mercury_cli/__init__.py | head -1)}"
[ -n "$VERSION" ] || VERSION=dev
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

build_one() { # $1 = arch suffix (x64|arm64), $2 = source binary path, $3 = label
    local ARCHSUF="$1" SRCBIN="$2" LABEL="$3"
    local OUT="dist/mercury-${VERSION}-${ARCHSUF}.tar.gz"
    local S="$STAGE/$ARCHSUF"
    echo "== [$LABEL] staging repo source (git archive = exactly what's committed)"
    mkdir -p "$S/mercury"
    git archive HEAD | tar -x -C "$S/mercury"

    echo "== [$LABEL] injecting prebuilt artifacts (gitignored, built by the release host)"
    mkdir -p "$S/mercury/omp/packages/coding-agent/dist"
    cp "$SRCBIN" "$S/mercury/omp/packages/coding-agent/dist/omp"
    mkdir -p "$S/mercury/hermes/ui-tui/dist"
    cp hermes/ui-tui/dist/entry.js "$S/mercury/hermes/ui-tui/dist/entry.js"
    # natives if present (rust-built .so/.node; runtime fallback path — the
    # primary natives are EMBEDDED in the compiled binary)
    if compgen -G "omp/packages/natives/native/*" >/dev/null; then
        mkdir -p "$S/mercury/omp/packages/natives/native"
        cp -r omp/packages/natives/native/. "$S/mercury/omp/packages/natives/native/"
    fi

    cat > "$S/mercury/DIST_INFO.txt" <<EOF
Mercury distribution
version:    ${VERSION}
arch:       ${ARCHSUF}
built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)
built-on:   $(uname -srm)
hermes pin: $(grep -m1 hermes PINS.txt || true)
omp pin:    $(grep -m1 '^omp' PINS.txt || true)
components: source (git archive $(git rev-parse --short HEAD)) + omp binary (${ARCHSUF}) + ui-tui bundle + natives
EOF

    echo "== [$LABEL] tarball"
    mkdir -p dist
    tar -czf "$OUT" -C "$S" mercury
    sha256sum "$OUT" > "${OUT}.sha256"
    echo "OK: $OUT ($(du -h "$OUT" | cut -f1))"
    echo "    checksum: ${OUT}.sha256"
}

[ -x omp/packages/coding-agent/dist/omp ] \
    || { echo "FATAL: x86 binary missing (build: bun run build in omp/packages/coding-agent)" >&2; exit 1; }
build_one x64 omp/packages/coding-agent/dist/omp "x86-64"

if [ -x omp/packages/coding-agent/dist/omp-linux-arm64 ]; then
    build_one arm64 omp/packages/coding-agent/dist/omp-linux-arm64 "aarch64"
else
    echo "WARNING: no arm64 binary — skipping arm64 tarball (build with CROSS_TARGET=linux-arm64)" >&2
fi
