#!/usr/bin/env bash
# build-mlounge-fork.sh — release-host build of the vendored mLounge fork.
#
# NEVER runs on user machines: it needs the full dev toolchain (vite,
# tsc) and takes minutes. Users get the prebuilt tree via the release
# tarball (see ensure_mlounge_installed); Pi installs never compile.
#
# Output: dist/mlounge-fork/tree/ — the complete runnable tree minus
# node_modules, plus .mercury-fork-build.json (fork version + source
# fingerprint + mercury rev) so make-dist.sh can fail hard on a stale
# payload instead of shipping last week's bundle.
#
# Usage: bash scripts/build-mlounge-fork.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$REPO/third_party/mlounge"
OUT="$REPO/dist/mlounge-fork/tree"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/mlounge-fork-build.XXXXXX")"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

[ -f "$SRC/package.json" ] || { echo "FATAL: no vendored fork at $SRC" >&2; exit 1; }
command -v node >/dev/null || { echo "FATAL: node required on release host" >&2; exit 1; }
command -v npm >/dev/null || { echo "FATAL: npm required on release host" >&2; exit 1; }
command -v npx >/dev/null || { echo "FATAL: npx required for pinned Yarn on release host" >&2; exit 1; }

echo "== staging fork source (excluding node_modules)"
cp -r "$SRC/." "$TMP/tree/"
rm -rf "$TMP/tree/node_modules" "$TMP/tree/.git"

echo "== Yarn 1.22.22 frozen install (full toolchain, release host only)"
(
    cd "$TMP/tree"
    npx --yes yarn@1.22.22 install --frozen-lockfile --non-interactive 2>&1 | tail -1
)

echo "== vite build (client) + tsc (server)"
(
    cd "$TMP/tree"
    npm run build 2>&1 | tail -3
)

[ -f "$TMP/tree/dist/server/index.js" ] \
    || { echo "FATAL: server build produced no dist/server/index.js" >&2; exit 1; }
FORK_VERSION="$(python3 -c "import json; print(json.load(open('$TMP/tree/package.json'))['version'])")"
python3 -c "import json,sys; sys.exit(0 if json.load(open('$TMP/tree/package.json')).get('mercuryFork') is True else 1)" \
    || { echo "FATAL: fork tree lacks the mercuryFork marker" >&2; exit 1; }
SOURCE_SHA="$(cd "$SRC" && find . -type f -not -path './node_modules/*' -not -path './.git/*' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
MERCURY_REV="$(cd "$REPO" && git rev-parse --short HEAD)"

echo "== content gates (a stale build must never ship)"
grep -q "draft/multiline" "$TMP/tree/dist/server/plugins/inputs/msg.js" \
    || { echo "FATAL: server bundle lacks fork send path" >&2; exit 1; }
grep -q "draft/multiline" "$TMP/tree/dist/server/plugins/irc-events/message.js" \
    || { echo "FATAL: server bundle lacks fork reassembly" >&2; exit 1; }
_client_bundle="$(find "$TMP/tree/public/assets" -maxdepth 1 -name 'index-*.js' | head -1)"
[ -n "$_client_bundle" ] \
    || { echo "FATAL: client build produced no public/assets/index-*.js" >&2; exit 1; }

echo "== staging payload"
rm -rf "$OUT"
mkdir -p "$OUT"
cp -r "$TMP/tree/." "$OUT/"
rm -rf "$OUT/node_modules"
cat > "$OUT/.mercury-fork-build.json" <<EOF
{"fork_version": "$FORK_VERSION", "source_sha": "$SOURCE_SHA", "mercury_rev": "$MERCURY_REV", "built_at_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
echo "OK: $OUT (fork $FORK_VERSION, source $SOURCE_SHA)"
