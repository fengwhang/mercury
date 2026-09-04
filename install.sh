#!/usr/bin/env bash
# Mercury installer — one command for the hybrid distribution.
#   curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | bash
# Combines what omp.sh/install and hermes' install.sh each did, without
# redundant steps: preflight -> deps (uv + venv from vendored pins) ->
# unpack (or git clone) -> four-slot model wizard (fail-hard) -> .env keys
# -> approval mode (incl. PERMANENT yolo) -> symlink. omp ships prebuilt
# in the tarball — no bun, no rust on target.
set -euo pipefail

INSTALL_ROOT="${MERCURY_INSTALL_ROOT:-$HOME/.local/share/mercury}"
BIN_DIR="${MERCURY_BIN_DIR:-$HOME/.local/bin}"
TARBALL_URL="${1:-}"
CONFIRM="${MERCURY_INSTALL_YES:-}"

say()  { printf '\033[1;35m🎚 mercury\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mmercury install failed:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { [ -n "$CONFIRM" ] && return 0; read -r -p "$1 " ans </dev/tty && [[ "$ans" =~ ^[Yy] ]]; }

# ---------------------------------------------------------------- preflight
say "preflight"
command -v curl >/dev/null || die "curl required"
command -v tar >/dev/null || die "tar required"
for TOOL in python3 git; do command -v "$TOOL" >/dev/null || say "note: $TOOL not found (uv will supply python; git only needed for clone mode)"; done
mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

# ---------------------------------------------------------------- source
if [ -n "$TARBALL_URL" ]; then
    say "fetching distribution tarball"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    curl -fsSL "$TARBALL_URL" -o "$TMP/mercury.tar.gz"
    say "unpacking to $INSTALL_ROOT"
    tar -xzf "$TMP/mercury.tar.gz" -C "$TMP"
    rsync -a --delete "$TMP/mercury/" "$INSTALL_ROOT/" 2>/dev/null || cp -r "$TMP/mercury/." "$INSTALL_ROOT/"
elif [ -x "$INSTALL_ROOT/bin/mercury" ]; then
    say "existing tree at $INSTALL_ROOT — reinstalling in place (keeping ~/.mercury state)"
else
    die "no tarball URL given and no existing tree at $INSTALL_ROOT. Usage: install.sh [tarball-url]"
fi

cd "$INSTALL_ROOT"
[ -f bin/mercury ] || die "distribution incomplete: bin/mercury missing"
[ -x omp/packages/coding-agent/dist/omp ] || die "distribution incomplete: prebuilt omp binary missing (build with scripts/make-dist.sh)"
[ -f hermes/ui-tui/dist/entry.js ] || die "distribution incomplete: ui-tui bundle missing (build with scripts/make-dist.sh)"

# ---------------------------------------------------------------- python deps
say "python environment (uv + exact-pinned venv)"
if ! command -v uv >/dev/null; then
    say "installing uv (single static binary, no root)"
    curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
fi
VENV="$INSTALL_ROOT/hermes/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    uv venv "$VENV" --python '>=3.11,<3.14' 2>/dev/null || uv venv "$VENV"
    ( cd hermes && uv pip install --python "$VENV/bin/python" -e . )
fi

# ---------------------------------------------------------------- model wizard
say "model configuration (four slots, fail-hard: no fallback configured = no start)"
cfg="$HOME/.mercury/config.yaml"
mkdir -p "$HOME/.mercury"
if [ ! -f "$cfg" ]; then
    get_slot() { # $1=varname $2=prompt $3=required
        local v=""
        while :; do
            read -r -p "$2: " v </dev/tty
            [ -n "$v" ] && break
            [ "$3" = required ] || { v=""; break; }
            echo "  (required)" >&2
        done
        printf '%s' "$v"
    }
    DEFAULT=$(get_slot default "default model (provider/model, e.g. zai/glm-5.3 or anthropic/claude-sonnet-4-6)" required)
    FALLBACK=$(get_slot fallback "fallback model (required — fail-hard policy)" required)
    DELEGATE=$(get_slot delegate "delegate model (Enter = same as default)")
    DELEGATE_FB=$(get_slot delegate_fb "delegate fallback (Enter = same as fallback)")
    [ -z "$DELEGATE" ] && DELEGATE="$DEFAULT"
    [ -z "$DELEGATE_FB" ] && DELEGATE_FB="$FALLBACK"
    [ "$DEFAULT" = "$FALLBACK" ] && die "default and fallback must differ (fail-hard check)"
    say "approval mode: manual (prompt everything) | smart (read+write auto-approved, exec prompts) | off (PERMANENT yolo — no prompts anywhere; deny-rules + hardline floor stay active)"
    read -r -p "approvals.mode [smart]: " am </dev/tty
    am="${am:-smart}"
    case "$am" in manual|smart|off) ;; *) am=smart ;; esac
    cat > "$cfg" <<EOF
approvals:
  mode: "$am"

models:
  default: $DEFAULT
  fallback: $FALLBACK
  delegate_model: $DELEGATE
  delegate_fallback: $DELEGATE_FB

hermes: {}
EOF
    say "unified config written: $cfg (approvals: $am)"
fi

# ---------------------------------------------------------------- keys
say "API keys (written to ~/.mercury/hermes/.env — env-only, never in the repo)"
ENVF="$HOME/.mercury/hermes/.env"
mkdir -p "$HOME/.mercury/hermes"
touch "$ENVF"; chmod 600 "$ENVF"
add_key() { # $1=VARNAME
    grep -q "^$1=" "$ENVF" && return 0
    read -r -p "$1: " v </dev/tty
    [ -n "$v" ] && printf '%s=%s\n' "$1" "$v" >> "$ENVF"
}
PROV="${DEFAULT%%/*}"
case "$PROV" in
    zai) add_key ZAI_API_KEY ;;
    openai) add_key OPENAI_API_KEY ;;
    anthropic) add_key ANTHROPIC_API_KEY ;;
    openrouter) add_key OPENROUTER_API_KEY ;;
    *) say "provider '$PROV' — add its key var to $ENVF yourself" ;;
esac

# ---------------------------------------------------------------- approvals
if ! grep -q '^approvals:' "$cfg"; then
    # pre-existing config without the knob (migrated installs)
    say "approval mode: manual (prompt everything) | smart (default) | off (PERMANENT yolo — deny-rules + hardline floor stay active)"
    read -r -p "approvals.mode [smart]: " am </dev/tty
    am="${am:-smart}"
    case "$am" in manual|smart|off) ;; *) am=smart ;; esac
    printf 'approvals:\n  mode: "%s"\n\n' "$am" | cat - "$cfg" > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
fi

# ---------------------------------------------------------------- seed user-editable defaults
say "seeding hand-editable defaults from config/ (never overwrites)"
for _seed in HERMES.md OMP.md SOUL.md MEMORY.md USER.md AGENTS.md; do
    [ -f "$HOME/.mercury/$_seed" ] || cp "$INSTALL_ROOT/config/$_seed" "$HOME/.mercury/$_seed" 2>/dev/null || true
done

# ---------------------------------------------------------------- skills
say "seeding the shared skills library (~/.mercury/skills) from the built-in set"
if [ -d "$INSTALL_ROOT/hermes/skills" ] && [ ! -d "$HOME/.mercury/skills" ]; then
    mkdir -p "$HOME/.mercury/skills"
    ( cd "$INSTALL_ROOT/hermes/skills" && tar cf - . ) | ( cd "$HOME/.mercury/skills" && tar xf - )
elif [ -d "$INSTALL_ROOT/hermes/skills" ]; then
    # existing library: copy only NEW top-level categories (never clobber user edits)
    for _cat in "$INSTALL_ROOT/hermes/skills"/*/; do
        _name="$(basename "$_cat")"
        [ -d "$HOME/.mercury/skills/$_name" ] || cp -r "$_cat" "$HOME/.mercury/skills/$_name"
    done
fi

# ---------------------------------------------------------------- symlink
say "linking mercury -> $BIN_DIR/mercury"
ln -sf "$INSTALL_ROOT/bin/mercury" "$BIN_DIR/mercury"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "add $BIN_DIR to PATH (e.g. in ~/.bashrc)" ;; esac

say "installed. FIRST RUN: mercury   (first boot inherits nothing — the wizard above already wrote your config and keys)"
