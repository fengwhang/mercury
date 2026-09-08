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
# Each tarball ALSO carries wheels/ — the observatory crypto stack
# (mautrix[encryption] pinned set + cp313 python-olm wheel; see the gate
# in build_one) so existing installs get E2EE via `mercury update`.
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

    echo "== [$LABEL] observatory crypto-stack wheels (E2EE)"
    # python-olm has NO cp313 wheel on PyPI (built reproducibly by
    # hermes/observatory/scripts/build_python_olm_wheel.sh); mautrix
    # [encryption] pins it. The release host stages the FULL pinned set
    # (mautrix[encryption]==0.21.1 deps + per-arch python-olm wheel) into
    # $MERCURY_WHEELS_DIR (default dist/wheels); wheels freshly built by
    # the e2ee script into hermes/observatory/scripts/dist are picked up
    # too (staged set wins on a name clash). Shipping them lets
    # `mercury update` and install.sh deliver E2EE offline — a release
    # WITHOUT them bricks e2ee on existing installs, hence the gate below.
    mkdir -p "$S/mercury/wheels"
    local WHEELS_STAGED=0 WHL
    for WHL in "$REPO"/hermes/observatory/scripts/dist/*.whl; do
        [ -f "$WHL" ] || continue
        cp "$WHL" "$S/mercury/wheels/"
        WHEELS_STAGED=1
    done
    for WHL in "$REPO/${MERCURY_WHEELS_DIR:-dist/wheels}"/*.whl; do
        [ -f "$WHL" ] || continue
        cp "$WHL" "$S/mercury/wheels/"   # explicitly staged set wins
        WHEELS_STAGED=1
    done
    if [ ! -d "$S/mercury/hermes/observatory" ]; then
        : # no observatory code in this archive — wheels not required
    elif [ "$WHEELS_STAGED" = 1 ]; then
        echo "    staged $(ls "$S/mercury/wheels" | wc -l) wheel(s) into wheels/"
    elif [ -n "${MERCURY_SKIP_OBS_WHEELS:-}" ]; then
        echo "WARNING: observatory code shipped WITHOUT crypto-stack wheels" >&2
        echo "         (MERCURY_SKIP_OBS_WHEELS set) — e2ee on py3.13 installs" >&2
        echo "         will hard-fail until the wheel is built on the host." >&2
    else
        echo "FATAL: observatory code is in the archive but no crypto-stack wheels" >&2
        echo "       were found — this release would ship broken E2EE (python-olm" >&2
        echo "       has no cp313 wheel on PyPI; uv would try to build it from the" >&2
        echo "       sdist and fail on most hosts)." >&2
        echo "       Stage the wheel set into ${MERCURY_WHEELS_DIR:-dist/wheels}/" >&2
        echo "       (and/or build with hermes/observatory/scripts/build_python_olm_wheel.sh)," >&2
        echo "       then re-run. To skip DELIBERATELY: MERCURY_SKIP_OBS_WHEELS=1." >&2
        exit 1
    fi


    cat > "$S/mercury/DIST_INFO.txt" <<EOF
Mercury distribution
version:    ${VERSION}
arch:       ${ARCHSUF}
built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)
built-on:   $(uname -srm)
hermes pin: $(grep -m1 hermes PINS.txt || true)
omp pin:    $(grep -m1 '^omp' PINS.txt || true)
components: source (git archive $(git rev-parse --short HEAD)) + omp binary (${ARCHSUF}) + ui-tui bundle + natives + observatory wheels
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
