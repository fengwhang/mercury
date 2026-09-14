#!/usr/bin/env bash
# build-soju.sh — build the pinned soju bouncer (+sojuctl/+sojudb) for
# both release arches, pure-Go (no CGO, static binaries).
#
# Output: build/soju/<arch>/soju{,ctl,db} (gitignored; make-dist.sh
# injects the matching triple into each per-arch tarball).
# Go toolchain: $HOME/go-dist (user-space, no sudo) > system go >
# download official tarball to $HOME/go-dist.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOJU_PIN="v0.10.1"
GO_VERSION="1.25.1"
SRC="$REPO/build/soju-src"
OUT="$REPO/build/soju"

_go_bin() {
    if [ -x "$HOME/go-dist/go/bin/go" ]; then
        echo "$HOME/go-dist/go/bin/go"
    elif command -v go >/dev/null 2>&1; then
        echo "go"
    else
        echo "installing Go $GO_VERSION to \$HOME/go-dist (user-space, no sudo)" >&2
        curl -sfL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
            -o /tmp/mercury-go.tar.gz
        mkdir -p "$HOME/go-dist"
        tar -xzf /tmp/mercury-go.tar.gz -C "$HOME/go-dist"
        rm -f /tmp/mercury-go.tar.gz
        echo "$HOME/go-dist/go/bin/go"
    fi
}

GO="$(_go_bin)"
export CGO_ENABLED=0 GOFLAGS="-tags=moderncsqlite"
(
    cd "$SRC"
    for arch in amd64 arm64; do
        mkdir -p "$OUT/$arch"
        for cmd in soju sojuctl sojudb; do
            GOOS=linux GOARCH="$arch" "$GO" build \
                -o "$OUT/$arch/$cmd" "./cmd/$cmd"
        done
        echo "soju $SOJU_PIN [$arch]: $(ls "$OUT/$arch")"
    done
)
echo "binaries in $OUT/<arch>/ (gitignored; packed by scripts/make-dist.sh)"
