#!/usr/bin/env bash
# make-dist.sh — assemble a self-contained Mercury distribution tarball.
# Output: dist/mercury-<version>.tar.gz containing the repo source (minus
# dev cruft) PLUS prebuilt artifacts (omp binary, ui-tui bundle) so a clean
# VM needs neither bun nor rust nor esbuild.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VERSION="${MERCURY_VERSION:-$(sed -n "s/^__version__ = \"\(.*\)\".*/\1/p" hermes/mercury_cli/__init__.py | head -1)}"
[ -n "$VERSION" ] || VERSION=dev
OUT="dist/mercury-${VERSION}.tar.gz"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "== staging repo source (git archive = exactly what's committed)"
mkdir -p "$STAGE/mercury"
git archive HEAD | tar -x -C "$STAGE/mercury"

echo "== injecting prebuilt artifacts (gitignored, built by the release host)"
# omp compiled binary (bun build + natives; 161MB)
mkdir -p "$STAGE/mercury/omp/packages/coding-agent/dist"
cp omp/packages/coding-agent/dist/omp "$STAGE/mercury/omp/packages/coding-agent/dist/omp"
# ui-tui bundle (esbuild)
mkdir -p "$STAGE/mercury/hermes/ui-tui/dist"
cp hermes/ui-tui/dist/entry.js "$STAGE/mercury/hermes/ui-tui/dist/entry.js"
# natives if present (rust-built .so/.node used by omp)
if compgen -G "omp/packages/natives/native/*" >/dev/null; then
    mkdir -p "$STAGE/mercury/omp/packages/natives/native"
    cp -r omp/packages/natives/native/. "$STAGE/mercury/omp/packages/natives/native/"
fi

echo "== writing VERSION + build manifest"
cat > "$STAGE/mercury/DIST_INFO.txt" <<EOF
Mercury distribution
version:    ${VERSION}
built:      $(date -u +%Y-%m-%dT%H:%M:%SZ)
built-on:   $(uname -srm)
hermes pin: $(grep -m1 hermes PINS.txt || true)
omp pin:    $(grep -m1 '^omp' PINS.txt || true)
components: source (git archive $(git rev-parse --short HEAD)) + omp binary + ui-tui bundle + natives
EOF

echo "== tarball"
mkdir -p dist
tar -czf "$OUT" -C "$STAGE" mercury
sha256sum "$OUT" > "${OUT}.sha256"

SIZE=$(du -h "$OUT" | cut -f1)
echo "OK: $OUT ($SIZE)"
echo "    checksum: ${OUT}.sha256"
