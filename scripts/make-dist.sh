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
# No wheels/, no tuwunel-binaries/: the IRC observatory daemon is
# stdlib-only, so virgin installs provision with zero network and zero
# staged binaries.
#
# RELEASE ORDER (build-after-bump — v0.0.19 lesson): the omp binary bakes
# MERCURY_VERSION at COMPILE time (compile-binary.ts resolveMercuryVersion
# reads hermes/mercury_cli/__init__.py __version__). A 0.0.19 build that ran
# BEFORE the __version__ bump shipped omp/0.0.18 inside a v0.0.19 tarball
# (--version and User-Agent both stale). Correct order is therefore:
#   1. bump __version__ in hermes/mercury_cli/__init__.py (+ commit),
#   2. rebuild the omp binary (bun run build [+ CROSS_TARGET]),
#   3. run this script (it fail-hards below if the binary is stale).
# Never build → bump → pack.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

FILE_VERSION="$(sed -n "s/^__version__ = \"\(.*\)\".*/\1/p" hermes/mercury_cli/__init__.py | head -1)"
VERSION="${MERCURY_VERSION:-$FILE_VERSION}"
[ -n "$VERSION" ] || VERSION=dev
# MERCURY_VERSION is an override, not a second source of truth: if it drifts
# from the committed __version__, the tarball name and the baked binary would
# disagree about the release. Fail here rather than shipping a mislabeled pack.
if [ -n "${MERCURY_VERSION:-}" ] && [ -n "$FILE_VERSION" ] && [ "$MERCURY_VERSION" != "$FILE_VERSION" ]; then
    echo "FATAL: MERCURY_VERSION=$MERCURY_VERSION != __version__=$FILE_VERSION (hermes/mercury_cli/__init__.py)" >&2
    echo "       The version bump must land BEFORE the omp build and the pack —" >&2
    echo "       rebuild the binary after the bump, then re-run without the override." >&2
    exit 1
fi
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Fail-hard gate: the omp binary's baked user-agent (omp/<ver>-mercury, via
# Bun.build define process.env.MERCURY_VERSION) must match the release being
# packed. Extract with `strings` (grep -a fallback) so a binary built BEFORE
# the __version__ bump can never ship (v0.0.19 packed omp/0.0.18 this way).
check_binary_version() { # $1 = binary path, $2 = label
    local BIN="$1" LABEL="$2"
    local EXPECTED="omp/${VERSION}-mercury"
    local HAY
    if command -v strings >/dev/null 2>&1; then
        HAY="$(strings "$BIN" | grep -F "$EXPECTED" || true)"
    else
        HAY="$(grep -a -F "$EXPECTED" "$BIN" || true)"
    fi
    if [ -z "$HAY" ]; then
        local ACTUAL
        if command -v strings >/dev/null 2>&1; then
            ACTUAL="$(strings "$BIN" | grep -E -o -m1 'omp/[0-9][0-9A-Za-z._-]*-mercury' || true)"
        else
            ACTUAL="$(grep -a -E -o -m1 'omp/[0-9][0-9A-Za-z._-]*-mercury' "$BIN" || true)"
        fi
        [ -n "$ACTUAL" ] || ACTUAL="(no omp/<ver>-mercury user-agent found)"
        echo "FATAL: [$LABEL] omp binary baked version mismatch: pack is $VERSION but binary reports $ACTUAL" >&2
        echo "       The binary was built BEFORE the __version__ bump (build-after-bump order:" >&2
        echo "       bump hermes/mercury_cli/__init__.py, rebuild omp, then re-run make-dist.sh)." >&2
        exit 1
    fi
    echo "    [$LABEL] omp binary version OK ($EXPECTED)"
}

build_one() { # $1 = arch suffix (x64|arm64), $2 = source binary path, $3 = label
    local ARCHSUF="$1" SRCBIN="$2" LABEL="$3"
    check_binary_version "$SRCBIN" "$LABEL" # re-gate per arch: no stale binary ships
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
components: source (git archive $(git rev-parse --short HEAD)) + omp binary (${ARCHSUF}) + ui-tui bundle + natives (IRC observatory: stdlib, nothing staged)
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

# Stable version-less aliases (hardlinks) so the one-liner never needs the
# version in its URL: releases/latest/download/mercury-<arch>.tar.gz always
# resolves. The installer constructs this URL itself when no URL is given.
ln -f "dist/mercury-${VERSION}-x64.tar.gz"     "dist/mercury-x64.tar.gz"
ln -f "dist/mercury-${VERSION}-x64.tar.gz.sha256" "dist/mercury-x64.tar.gz.sha256"
if [ -f "dist/mercury-${VERSION}-arm64.tar.gz" ]; then
    ln -f "dist/mercury-${VERSION}-arm64.tar.gz"     "dist/mercury-arm64.tar.gz"
    ln -f "dist/mercury-${VERSION}-arm64.tar.gz.sha256" "dist/mercury-arm64.tar.gz.sha256"
fi
echo "aliases: mercury-x64.tar.gz mercury-arm64.tar.gz (version-less, latest)"
