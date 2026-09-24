#!/bin/bash
# Upload a release's dist/ assets (versioned + version-less aliases).
# The installer one-liner fetches mercury-{x64,arm64}.tar.gz from
# releases/latest — a release WITHOUT those alias assets 404s the
# installer for everyone (v0.0.135-v0.0.137 shipped without them).
#
# Usage: GITHUB_TOKEN=... bash scripts/upload-dist.sh <tag> [dist-dir]
#   e.g. GITHUB_TOKEN=... bash scripts/upload-dist.sh v0.0.138 dist
set -u
TAG="${1:?usage: upload-dist.sh <tag> [dist-dir]}"
DIST="${2:-dist}"
[ -z "${GITHUB_TOKEN:-}" ] && { echo "GITHUB_TOKEN is required" >&2; exit 1; }

RID="$(curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/Fengwhang/mercury/releases/tags/$TAG" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")" \
    || { echo "no release for tag $TAG" >&2; exit 1; }

have() { # asset already attached?
    curl -sf -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/Fengwhang/mercury/releases/$RID/assets?per_page=100" \
        | python3 -c "import json,sys; print(any(a['name']=='$1' for a in json.load(sys.stdin)))"
}

# The installer one-liner fetches these — refuse to publish without them.
for alias in mercury-x64.tar.gz mercury-arm64.tar.gz mercury-x64.tar.gz.sha256 mercury-arm64.tar.gz.sha256; do
    [ -f "$DIST/$alias" ] || { echo "missing required alias asset: $DIST/$alias (run make-dist.sh)" >&2; exit 1; }
done

for f in "$DIST"/mercury-*.tar.gz "$DIST"/mercury-*.tar.gz.sha256; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    if [ "$(have "$name")" = "True" ]; then
        echo "skip $name (already attached)"
        continue
    fi
    id="$(curl -sf -X POST -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Content-Type: application/gzip" --data-binary "@$f" \
        "https://uploads.github.com/repos/Fengwhang/mercury/releases/$RID/assets?name=$name" \
        --no-progress-meter | python3 -c "import json,sys; print(json.load(sys.stdin).get('id'))")" \
        && echo "uploaded $name ($id)" || { echo "FAILED $name" >&2; fail=1; }
done

[ "$fail" = 0 ] || exit 1
echo "done: release $TAG processed (dist/ holds versioned + alias assets)"
