#!/usr/bin/env bash
# ============================================================================
# Mercury Installer — the hybrid distribution (Hermes + omp)
# ============================================================================
# One command for both engines. Modeled on the installers of both parents:
# hermes' (uv + venv + system deps + interactive setup) and omp's (prebuilt
# binary + arch detection + smoke test), combined without redundant steps.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | bash -s -- <tarball-url>
#   bash install.sh <tarball-url>     # from a checkout
#   bash install.sh                   # reinstall in place
#
# Options:
#   --tarball URL     Distribution tarball (else $1, else in-place)
#   --dir PATH        Installation directory (default ~/.local/share/mercury)
#   --non-interactive Skip all questions; sensible defaults
#   --yes             Alias for --non-interactive
#   --skip-browser    Skip Playwright/Chromium (browser tools off)
#   --skip-computer-use  Skip cua-driver (desktop control off)
#   --no-skills       Blank slate — seed no bundled skills
#   --ensure DEPS     Install only these deps: node,browser,ripgrep,ffmpeg
# ============================================================================
set -euo pipefail

# --- env hygiene (hermes installer lesson: leaking PYTHONPATH breaks pip) ---
if [ -n "${PYTHONPATH:-}" ]; then echo "⚠ ignoring inherited PYTHONPATH"; unset PYTHONPATH; fi
if [ -n "${PYTHONHOME:-}" ]; then echo "⚠ ignoring inherited PYTHONHOME"; unset PYTHONHOME; fi
export UV_NO_CONFIG=1

# --- colors / logging ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; MAGENTA='\033[1;35m'; NC='\033[0m'
log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

# --- banner ---
echo -e "\n${MAGENTA}┌──────────────────────────────────────────────┐${NC}"
echo -e "${MAGENTA}│            🌡 Mercury Installer                │${NC}"
echo -e "${MAGENTA}│   Hermes + omp — one agent, one config        │${NC}"
echo -e "${MAGENTA}│   Mercury is a hybrid distribution of         │${NC}"
echo -e "${MAGENTA}│   Hermes by Nous Research and omp by can1357  │${NC}"
echo -e "${MAGENTA}└──────────────────────────────────────────────┘${NC}\n"

# --- config ---
INSTALL_ROOT="${MERCURY_INSTALL_ROOT:-$HOME/.local/share/mercury}"
BIN_DIR="${MERCURY_BIN_DIR:-$HOME/.local/bin}"
TARBALL_URL=""
NON_INTERACTIVE=false
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
NO_SKILLS=false
ENSURE_DEPS=""
OS=""; DISTRO=""; ARCH=""

# --- interactive detection (curl | bash has no tty on stdin) ---
IS_INTERACTIVE=true
[ -t 0 ] || IS_INTERACTIVE=false

# --- args ---
while [[ $# -gt 0 ]]; do
    case $1 in
        --tarball) TARBALL_URL="$2"; shift 2 ;;
        --dir) INSTALL_ROOT="$2"; shift 2 ;;
        --non-interactive|--yes|-y) NON_INTERACTIVE=true; shift ;;
        --skip-browser|--no-playwright) SKIP_BROWSER=true; shift ;;
        --skip-computer-use) SKIP_COMPUTER_USE=true; shift ;;
        --no-skills) NO_SKILLS=true; shift ;;
        --ensure) ENSURE_DEPS="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)
            if [ -z "$TARBALL_URL" ] && { [[ "$1" == http* ]] || [ -f "$1" ]; }; then TARBALL_URL="$1"; shift
            else echo "Unknown option: $1"; exit 1; fi ;;
    esac
done

# --- prompting that works under curl|bash (reads /dev/tty like hermes') ---
prompt() { # $1=question $2=default(yes/no)
    local q="$1" def="${2:-yes}" suffix answer=""
    case "$def" in y|Y|yes|true) suffix="[Y/n]" ;; *) suffix="[y/N]" ;; esac
    if [ "$NON_INTERACTIVE" = true ]; then answer=""
    elif [ "$IS_INTERACTIVE" = true ]; then read -r -p "$q $suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$q" "$suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    fi
    [ -z "$answer" ] && case "$def" in y|Y|yes|true) return 0 ;; *) return 1 ;; esac
    case "$answer" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}
prompt_value() { # $1=question $2=default  -> echoes answer
    local q="$1" def="${2:-}" answer=""
    if [ "$NON_INTERACTIVE" = true ]; then echo "$def"; return 0; fi
    if [ "$IS_INTERACTIVE" = true ]; then read -r -p "$q [$def]: " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s [%s]: " "$q" "$def" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    fi
    echo "${answer:-$def}"
}

# ============================================================================
# system detection (omp's arch rigor + hermes' distro detection)
# ============================================================================
detect_system() {
    case "$(uname -s)" in
        Linux*) OS="linux"
            [ -f /etc/os-release ] && . /etc/os-release && DISTRO="${ID:-unknown}" || DISTRO="unknown"
            [ -n "${TERMUX_VERSION:-}" ] && DISTRO="termux"
            ;;
        Darwin*) OS="macos"; DISTRO="macos" ;;
        *) log_error "Unsupported OS: $(uname -s)"; exit 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) ARCH="x64" ;;
        arm64|aarch64) ARCH="arm64" ;;
        *) log_error "Unsupported architecture: $(uname -m)"; exit 1 ;;
    esac
    log_success "Detected: $OS ($DISTRO) $ARCH"
    if [ "$OS" = "linux" ] && { [ -f /etc/alpine-release ] || { command -v ldd >/dev/null 2>&1 && ldd --version 2>&1 | grep -qi musl; }; }; then
        log_warn "musl system detected — the omp binary links libstdc++/libgcc dynamically:"
        log_warn "  if it fails to start: apk add libstdc++ libgcc"
    fi
}

# ============================================================================
# dependencies (hermes' managed-uv pattern)
# ============================================================================
install_uv() {
    local managed="$HOME/.local/bin/uv"
    if [ -x "$managed" ]; then log_success "uv found ($("$managed" --version 2>/dev/null)"; return 0; fi
    if command -v uv >/dev/null 2>&1; then log_success "uv found on PATH"; return 0; fi
    log_info "Installing uv (no root needed)..."
    local installer logf
    installer="$(mktemp)"; logf="$(mktemp)"
    curl -LsSf https://astral.sh/uv/install.sh -o "$installer" 2>"$logf" || { log_error "uv download failed"; cat "$logf" >&2; exit 1; }
    UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh "$installer" >>"$logf" 2>&1 || { log_error "uv install failed"; cat "$logf" >&2; exit 1; }
    [ -x "$managed" ] || { log_error "uv installer reported success but no binary"; exit 1; }
    log_success "uv installed"
}

install_system_packages() {
    # ripgrep + ffmpeg: hermes' optional tool deps; best-effort like upstream
    local missing=""
    command -v rg  >/dev/null 2>&1 || missing="$missing ripgrep"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    [ -z "$missing" ] && { log_success "system tools present (rg, ffmpeg)"; return 0; }
    log_info "missing system tools:$missing"
    if [ "$NON_INTERACTIVE" = true ]; then TRY_SYS=true
    else prompt "Install missing system packages (needs sudo where applicable)?" yes && TRY_SYS=true || TRY_SYS=false; fi
    [ "${TRY_SYS:-false}" = true ] || { log_warn "skipping; some hermes-side tools (search/media) will be limited"; return 0; }
    local sudo_cmd=""
    [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && sudo_cmd="sudo"
    case "$DISTRO" in
        ubuntu|debian) $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
                       $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ripgrep ffmpeg >/dev/null 2>&1 || log_warn "apt install failed — continuing" ;;
        fedora)        $sudo_cmd dnf install -y ripgrep ffmpeg >/dev/null 2>&1 || log_warn "dnf install failed — continuing" ;;
        macos)         command -v brew >/dev/null 2>&1 && brew install ripgrep ffmpeg >/dev/null 2>&1 || log_warn "brew missing/failed — continuing" ;;
        *)             log_warn "no auto-install for $DISTRO — install:$missing" ;;
    esac
}

install_browser() {
    # Playwright/Chromium for hermes-side browser tools (omp ships its own)
    if [ "$SKIP_BROWSER" = true ]; then log_info "skipping browser (--skip-browser)"; return 0; fi
    if [ -d "$HOME/.cache/ms-playwright" ] && ls "$HOME/.cache/ms-playwright"/chromium-* >/dev/null 2>&1; then
        log_success "Playwright browsers already present"; return 0; fi
    if [ "$NON_INTERACTIVE" = true ]; then
        log_info "non-interactive: skipping browser install (enable later: mercury tools)"; return 0
    fi
    prompt "Install Playwright/Chromium now for browser tools (~300MB)?" yes || { log_info "skipping browser (enable later via 'mercury tools')"; return 0; }
    VENV="$INSTALL_ROOT/hermes/.venv"
    [ -x "$VENV/bin/python" ] || return 0
    "$VENV/bin/python" -m playwright install chromium 2>&1 | tail -1 || log_warn "browser install failed — run 'mercury tools' later"
}

install_computer_use() {
    if [ "$SKIP_COMPUTER_USE" = true ]; then log_info "skipping computer-use"; return 0; fi
    if [ "$NON_INTERACTIVE" = true ]; then return 0; fi
    command -v cua-driver >/dev/null 2>&1 && { log_success "cua-driver present"; return 0; }
    prompt "Install cua-driver for desktop control?" no || return 0
    VENV="$INSTALL_ROOT/hermes/.venv"
    [ -x "$VENV/bin/python" ] || return 0
    "$VENV/bin/pip" install --quiet cua-driver 2>/dev/null || log_warn "cua-driver install failed — see 'mercury computer-use install'"
}

# ============================================================================
# source + python
# ============================================================================
fetch_tarball() {
    if [ -n "$TARBALL_URL" ]; then
        log_info "fetching distribution tarball"
        TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
        if [ -f "$TARBALL_URL" ]; then
            cp "$TARBALL_URL" "$TMP/mercury.tar.gz"
        else
            curl -fsSL --connect-timeout 10 --speed-limit 1024 --speed-time 60 "$TARBALL_URL" -o "$TMP/mercury.tar.gz" \
                || { log_error "tarball download failed"; exit 1; }
        fi
        # checksum when sibling .sha256 exists (URL or local path)
        local SHA_SRC=""
        if [ -f "${TARBALL_URL}.sha256" ]; then SHA_SRC="${TARBALL_URL}.sha256"
        elif [[ "$TARBALL_URL" == http* ]] && curl -fsSL "${TARBALL_URL}.sha256" -o "$TMP/mercury.tar.gz.sha256" 2>/dev/null; then SHA_SRC="$TMP/mercury.tar.gz.sha256"; fi
        if [ -n "$SHA_SRC" ]; then
            local want have
            want="$(awk '{print $1}' "$SHA_SRC" | head -1)"
            have="$(sha256sum "$TMP/mercury.tar.gz" | awk '{print $1}')"
            [ "$want" = "$have" ] && log_success "checksum verified" \
                || { log_error "CHECKSUM MISMATCH — corrupt download"; exit 1; }
        fi
        log_info "unpacking to $INSTALL_ROOT"
        tar -xzf "$TMP/mercury.tar.gz" -C "$TMP"
        mkdir -p "$INSTALL_ROOT"
        rsync -a --delete "$TMP/mercury/" "$INSTALL_ROOT/" 2>/dev/null || cp -r "$TMP/mercury/." "$INSTALL_ROOT/"
    elif [ -x "$INSTALL_ROOT/bin/mercury" ]; then
        log_info "existing tree at $INSTALL_ROOT — reinstalling in place (~/.mercury state kept)"
    else
        log_error "no tarball URL and no existing tree at $INSTALL_ROOT"
        log_info  "usage: install.sh <tarball-url>   (or --tarball URL)"; exit 1
    fi
    cd "$INSTALL_ROOT"
    [ -f bin/mercury ] || { log_error "distribution incomplete: bin/mercury missing"; exit 1; }
    [ -x omp/packages/coding-agent/dist/omp ] || { log_error "incomplete: prebuilt omp binary missing"; exit 1; }
    [ -f hermes/ui-tui/dist/entry.js ] || { log_error "incomplete: ui-tui bundle missing"; exit 1; }
}

setup_venv() {
    log_info "python environment (uv + exact-pinned venv)"
    local VENV="$INSTALL_ROOT/hermes/.venv"
    if [ ! -x "$VENV/bin/python" ]; then
        uv venv "$VENV" --python '>=3.11,<3.14' 2>/dev/null || uv venv "$VENV"
        ( cd hermes && uv pip install --python "$VENV/bin/python" -q -e . ) || { log_error "python deps failed"; exit 1; }
    fi
    log_success "python environment ready"
}

# ============================================================================
# smoke test (omp's lesson: never claim success for a binary that can't run)
# ============================================================================
smoke_test() {
    log_info "smoke test: engines start"
    omp/packages/coding-agent/dist/omp --version >/dev/null 2>&1 || { log_error "omp binary failed to start"; exit 1; }
    log_success "omp binary starts ($(omp/packages/coding-agent/dist/omp --version 2>/dev/null | head -1))"
    local VENV="$INSTALL_ROOT/hermes/.venv"
    "$VENV/bin/python" -c "import sys; sys.path.insert(0, '.'); import mercury_cli.main" 2>/dev/null \
        || { log_error "hermes CLI import failed"; exit 1; }
    log_success "hermes CLI imports"
}

# ============================================================================
# wizard (the interactive heart — mirrors hermes setup's question set,
# mercury-specific: four slots, one approvals knob, both engines' surfaces)
# ============================================================================
run_wizard() {
    local cfg="$HOME/.mercury/config.yaml"
    mkdir -p "$HOME/.mercury"

    if [ -f "$cfg" ]; then
        log_info "existing config at $cfg — keeping it (edit by hand or delete to re-wizard)"
        # still ensure the approvals knob exists on migrated configs
        if ! grep -q '^approvals:' "$cfg"; then
            local am; am="$(prompt_value "approvals.mode (manual|smart|off=PERMANENT yolo)" smart)"
            case "$am" in manual|smart|off) ;; *) am=smart ;; esac
            printf 'approvals:\n  mode: "%s"\n\n' "$am" | cat - "$cfg" > "$cfg.tmp" && mv "$cfg.tmp" "$cfg"
        fi
        return 0
    fi

    echo ""
    log_info "model configuration — four slots, fail-hard (no fallback = no start)"
    local DEFAULT FALLBACK DELEGATE DELEGATE_FB
    DEFAULT="$(prompt_value "default model (provider/model, e.g. zai/glm-5.3 or anthropic/claude-sonnet-4-6)" "")"
    while [ -z "$DEFAULT" ]; do log_warn "required"; DEFAULT="$(prompt_value "default model" "")"; done
    FALLBACK="$(prompt_value "fallback model (required — fail-hard)" "")"
    while [ -z "$FALLBACK" ]; do log_warn "required"; FALLBACK="$(prompt_value "fallback model" "")"; done
    [ "$DEFAULT" = "$FALLBACK" ] && { log_error "default and fallback must differ"; exit 1; }
    DELEGATE="$(prompt_value "delegate model (omp subagents; Enter = default)" "$DEFAULT")"
    DELEGATE_FB="$(prompt_value "delegate fallback (Enter = fallback)" "$FALLBACK")"

    echo ""
    log_info "approvals — ONE mode for both engines"
    echo "  manual: prompt for write/exec everywhere"
    echo "  smart:  read+workspace-write auto-approved; exec prompts   [default]"
    echo "  off:    PERMANENT yolo — no prompts anywhere (deny-rules + hardline floor stay active)"
    local am; am="$(prompt_value "approvals.mode" smart)"
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
    log_success "config written: $cfg (approvals: $am)"

    # --- keys (hermes-style: offer the common ones, write to .env) ---
    echo ""
    log_info "API keys — written to ~/.mercury/hermes/.env (chmod 600, never in the repo; Enter skips)"
    local ENVF="$HOME/.mercury/hermes/.env"
    mkdir -p "$HOME/.mercury/hermes"; touch "$ENVF"; chmod 600 "$ENVF"
    add_key() {
        grep -q "^$1=" "$ENVF" && return 0
        local v; v="$(prompt_value "  $1" "")"
        [ -n "$v" ] && printf '%s=%s\n' "$1" "$v" >> "$ENVF" && log_success "  $1 saved"
    }
    local prov="${DEFAULT%%/*}"
    case "$prov" in
        zai) add_key ZAI_API_KEY ;;
        openai) add_key OPENAI_API_KEY ;;
        anthropic) add_key ANTHROPIC_API_KEY ;;
        openrouter) add_key OPENROUTER_API_KEY ;;
        *) log_info "  provider '$prov' — add its key to $ENVF" ;;
    esac
    prompt "Also set a web-search/scrape key now (FIRECRAWL_API_KEY — both engines)?" no && add_key FIRECRAWL_API_KEY
    prompt "Also set ZAI_API_KEY for zai web search (both engines)?" no && add_key ZAI_API_KEY

    # --- surfaces (hermes' gateway question, mercury-scoped) ---
    echo ""
    if prompt "Set up a messaging gateway now (Telegram/SimpleX/Discord/... — 'mercury gateway')?" no; then
        log_info "run after install:  mercury gateway setup && mercury gateway start"
    fi
}

# ============================================================================
# seeds + path
# ============================================================================
seed_defaults() {
    log_info "seeding hand-editable defaults from config/ (never overwrites)"
    for _seed in HERMES.md OMP.md SOUL.md MEMORY.md USER.md AGENTS.md; do
        [ -f "$HOME/.mercury/$_seed" ] || cp "$INSTALL_ROOT/config/$_seed" "$HOME/.mercury/$_seed" 2>/dev/null || true
    done
    if [ "$NO_SKILLS" = true ]; then
        log_info "--no-skills: seeding no bundled skills"
    elif [ -d "$INSTALL_ROOT/hermes/skills" ]; then
        if [ ! -d "$HOME/.mercury/skills" ]; then
            log_info "seeding the shared skills library (~/.mercury/skills)"
            mkdir -p "$HOME/.mercury/skills"
            ( cd "$INSTALL_ROOT/hermes/skills" && tar cf - . ) | ( cd "$HOME/.mercury/skills" && tar xf - )
        else
            for _cat in "$INSTALL_ROOT/hermes/skills"/*/; do
                _name="$(basename "$_cat")"
                [ -d "$HOME/.mercury/skills/$_name" ] || cp -r "$_cat" "$HOME/.mercury/skills/$_name"
            done
        fi
    fi
}

setup_path() {
    mkdir -p "$BIN_DIR"
    ln -sf "$INSTALL_ROOT/bin/mercury" "$BIN_DIR/mercury"
    case ":$PATH:" in *":$BIN_DIR:"*) log_success "mercury command: $BIN_DIR/mercury" ;;
        *) log_warn "add $BIN_DIR to PATH (e.g. echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc)" ;; esac
}

print_success() {
    echo ""
    echo -e "${GREEN}✓ Mercury installed.${NC}"
    echo "  First run:   mercury"
    echo "  omp engine:  mercury omp"
    echo "  Docs:        https://github.com/fengwhang/mercury"
    echo ""
    echo "  Mercury is a hybrid distribution of Hermes by Nous Research"
    echo "  and omp by can1357."
}

# ============================================================================
# main
# ============================================================================
main() {
    detect_system
    install_uv
    fetch_tarball
    setup_venv
    smoke_test
    install_system_packages
    install_browser
    install_computer_use
    run_wizard
    seed_defaults
    setup_path
    print_success
}

if [ -n "$ENSURE_DEPS" ]; then
    # --ensure node,browser,ripgrep,ffmpeg — deps only, no install
    detect_system
    OLDROOT="$INSTALL_ROOT"; INSTALL_ROOT="${MERCURY_INSTALL_ROOT:-$HOME/.local/share/mercury}"
    for d in ${ENSURE_DEPS//,/ }; do
        case $d in
            ripgrep|ffmpeg) ENSURE_ONE="$d" ;;
            browser) install_browser ;;
            *) log_warn "unknown --ensure dep: $d" ;;
        esac
    done
    exit 0
fi

main
