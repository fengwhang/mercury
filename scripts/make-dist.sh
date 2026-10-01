#!/usr/bin/env bash
# make-dist.sh — assemble self-contained Mercury distribution tarballs.
# Output: PER-ARCH tarballs (user directive 2026-09-05: one binary per
# download, no dead weight):
#   dist/mercury-<version>-x64.tar.gz     (omp x86-64 prebuilt as dist/omp)
#   dist/mercury-<version>-arm64.tar.gz   (omp aarch64 prebuilt as dist/omp)
# Each contains the repo source (minus dev cruft) PLUS exactly ONE omp
# binary + ui-tui bundle so a clean VM needs neither bun nor rust
# nor esbuild. The release host cross-compiles the arm64 omp binary via
# CROSS_TARGET and stages both prebuilt sets (gitignored) into each
# tarball. No soju: the stdlib MIRC daemon owns the client ports.
# No wheels: the MIRC observatory daemon is stdlib-only, so virgin
# installs provision with zero network.
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

# Fail-hard gate: the pi-natives .node files embedded by `bun run build`
# must carry the version sentinel of packages/natives/package.json. A
# `curl .../latest/download` remediation (or any ad-hoc fetch) drops NEWER
# binaries into an older tree; the build then embeds them, the loader
# rejects them at runtime (sentinel mismatch), and every omp child dies
# (v0.0.80 shipped 18_1_16 binaries with an 18.1.6 loader this way).
check_natives_sentinel() {
    local PKGVER SENT EXPECTED f GOT
    PKGVER="$(sed -n 's/^[[:space:]]*"version":[[:space:]]*"\(.*\)".*/\1/p' omp/packages/natives/package.json | head -1)"
    [ -n "$PKGVER" ] || { echo "FATAL: cannot read omp/packages/natives/package.json version" >&2; exit 1; }
    EXPECTED="__piNativesV$(printf '%s' "$PKGVER" | tr -c 'A-Za-z0-9' '_')"
    for f in omp/packages/natives/native/*.node omp/packages/natives/native/musl/*.node; do
        [ -e "$f" ] || continue
        if command -v strings >/dev/null 2>&1; then
            GOT="$(strings "$f" | grep -o -m1 '__piNativesV[A-Za-z0-9_]*' || true)"
        else
            GOT="$(grep -a -o -m1 '__piNativesV[A-Za-z0-9_]*' "$f" || true)"
        fi
        if [ "$GOT" != "$EXPECTED" ]; then
            echo "FATAL: natives skew: $f carries sentinel '${GOT:-(none)}' but package.json is $PKGVER (want $EXPECTED)" >&2
            echo "       Fetch the matching platform binaries (npm pack @oh-my-pi/pi-natives-<tag>@$PKGVER)" >&2
            echo "       into omp/packages/natives/native/ and rebuild the omp binary." >&2
            exit 1
        fi
    done
    echo "    natives sentinel OK ($EXPECTED)"
}
check_natives_sentinel

build_one() { # $1 = arch suffix (x64|arm64), $2 = source binary path, $3 = label
    local ARCHSUF="$1" SRCBIN="$2" LABEL="$3" LIBC="${4:-glibc}"
    local SUFFIX="$ARCHSUF"
    [ "$LIBC" = "musl" ] && SUFFIX="musl-$ARCHSUF"
    check_binary_version "$SRCBIN" "$LABEL" # re-gate per arch: no stale binary ships
    local OUT="dist/mercury-${VERSION}-${SUFFIX}.tar.gz"
    local S="$STAGE/$SUFFIX"
    echo "== [$LABEL] staging repo source (git archive = exactly what's committed)"
    mkdir -p "$S/mercury"
    git archive HEAD | tar -x -C "$S/mercury"

    echo "== [$LABEL] injecting prebuilt artifacts (gitignored, built by the release host)"
    mkdir -p "$S/mercury/omp/packages/coding-agent/dist"
    cp "$SRCBIN" "$S/mercury/omp/packages/coding-agent/dist/omp"
    mkdir -p "$S/mercury/hermes/ui-tui/dist"
    cp hermes/ui-tui/dist/entry.js "$S/mercury/hermes/ui-tui/dist/entry.js"
    # No soju triple: the stdlib MIRC daemon owns the client ports — nothing to inject.
    # natives if present (rust-built .so/.node; runtime fallback path — the
    # primary natives are EMBEDDED in the compiled binary)
    local NATIVE_DIR="omp/packages/natives/native" addon
    [ "$LIBC" = "musl" ] && NATIVE_DIR="$NATIVE_DIR/musl"
    mkdir -p "$S/mercury/omp/packages/natives/native"
    for addon in "$NATIVE_DIR"/pi_natives.linux-"$ARCHSUF"*.node; do
        [ -f "$addon" ] || continue
        cp "$addon" "$S/mercury/omp/packages/natives/native/"
    done
    # Mercury mLounge fork (prebuilt by scripts/build-mlounge-fork.sh on the
    # release host — user machines never compile). Fail hard on a missing
    # or stale payload instead of shipping last week's bundle.
    echo "== [$LABEL] injecting mLounge fork payload"
    _fork_src="third_party/mlounge"
    _fork_payload="dist/mlounge-fork/tree"
    [ -d "$_fork_payload" ] || { echo "FATAL: mLounge fork payload missing (run bash scripts/build-mlounge-fork.sh)" >&2; exit 1; }
    _want_ver="$(python3 -c "import json; print(json.load(open('$_fork_src/package.json'))['version'])")"
    _have_ver="$(python3 -c "import json; print(json.load(open('$_fork_payload/package.json'))['version'])")"
    [ "$_want_ver" = "$_have_ver" ] || { echo "FATAL: mLounge payload stale (payload $_have_ver != source $_want_ver) — rebuild" >&2; exit 1; }
    _want_sha="$(cd "$_fork_src" && find . -type f -not -path './node_modules/*' -not -path './.git/*' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
    _have_sha="$(python3 -c "import json; print(json.load(open('$_fork_payload/.mercury-fork-build.json'))['source_sha'])")"
    [ "$_want_sha" = "$_have_sha" ] || { echo "FATAL: mLounge payload source drift — rebuild" >&2; exit 1; }
    cp -r "$_fork_payload/." "$S/mercury/third_party/mlounge/"
    cat > "$S/mercury/DIST_INFO.txt" <<EOF
Mercury distribution
version:    ${VERSION}
arch:       ${ARCHSUF}
libc:       ${LIBC}
built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)
built-on:   $(uname -srm)
hermes pin: $(grep -m1 hermes PINS.txt || true)
omp pin:    $(grep -m1 '^omp' PINS.txt || true)
components: source (git archive $(git rev-parse --short HEAD)) + omp binary (${ARCHSUF}) + ui-tui bundle + natives + mLounge fork (prebuilt)
EOF

    echo "== [$LABEL] tarball"
    mkdir -p dist
    tar -czf "$OUT" -C "$S" mercury
    sha256sum "$OUT" > "${OUT}.sha256"
    echo "OK: $OUT ($(du -h "$OUT" | cut -f1))"
    echo "    checksum: ${OUT}.sha256"
}

# Validate every selected artifact before writing any tarball. A build-host
# smoke test cannot detect a missing loader on another distribution.
SPECS=("x64:glibc:omp:x86-64")
for spec in "arm64:glibc:omp-linux-arm64:aarch64" "x64:musl:omp-linux-musl-x64:musl-x86-64" "arm64:musl:omp-linux-musl-arm64:musl-aarch64"; do
    IFS=: read -r arch libc binary label <<< "$spec"
    if [ -x "omp/packages/coding-agent/dist/$binary" ]; then
        SPECS+=("$spec")
    else
        echo "WARNING: no $label binary — skipping (build with the matching CROSS_TARGET)" >&2
    fi
done
for spec in "${SPECS[@]}"; do
    IFS=: read -r arch libc binary label <<< "$spec"
    binary="omp/packages/coding-agent/dist/$binary"
    [ -x "$binary" ] || { echo "FATAL: $label binary missing" >&2; exit 1; }
    check_binary_version "$binary" "$label"
    python3 hermes/mercury_cli/elf.py "$binary" --arch "$arch" --libc "$libc" --portable || exit 1
    native_dir="omp/packages/natives/native"
    [ "$libc" = musl ] && native_dir="$native_dir/musl"
    count=0
    for addon in "$native_dir"/pi_natives.linux-"$arch"*.node; do
        [ -f "$addon" ] || continue
        python3 hermes/mercury_cli/elf.py "$addon" --arch "$arch" --libc "$libc" --portable --addon || exit 1
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || { echo "FATAL: no matching $arch/$libc native addon" >&2; exit 1; }
done
for spec in "${SPECS[@]}"; do
    IFS=: read -r arch libc binary label <<< "$spec"
    build_one "$arch" "omp/packages/coding-agent/dist/$binary" "$label" "$libc"
    suffix="$arch"
    [ "$libc" = musl ] && suffix="musl-$arch"
    ln -f "dist/mercury-${VERSION}-${suffix}.tar.gz" "dist/mercury-${suffix}.tar.gz"
    ln -f "dist/mercury-${VERSION}-${suffix}.tar.gz.sha256" "dist/mercury-${suffix}.tar.gz.sha256"
done
