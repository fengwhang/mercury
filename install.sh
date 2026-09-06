#!/bin/bash
# ============================================================================
# Mercury Installer — the hybrid distribution (Hermes + omp)
# ============================================================================
# One command for both engines. Modeled on the installers of both parents:
# hermes' (managed uv + venv + Browser Use CLI + cua-driver + `hermes setup`
# wizard with provider OAuth/Nous Portal + gateway) and omp's (prebuilt
# binary, arch rigor, checksum, smoke test) — combined, redundancies stripped
# (model provider asked ONCE, via `mercury setup`).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fengwhang/mercury/main/install.sh | bash -s -- <tarball-url>
#   bash install.sh <tarball-url>     # from a checkout
#   bash install.sh                   # reinstall in place
#
# Options:
#   --tarball URL        Distribution tarball (else $1, else in-place)
#   --dir PATH           Installation directory (default ~/.local/share/mercury)
#   --skip-setup         Skip the interactive setup wizard
#   --non-interactive|--yes   Non-interactive: no wizard, no questions
#   --skip-browser       Skip Browser Use CLI + Chromium (browser tools off)
#   --skip-computer-use  Skip the cua-driver (desktop control off)
#   --no-skills          Blank slate — seed no bundled skills
#   --skip-gateway       Skip the gateway install question
#   --ensure DEPS        Install only these deps: browser,computer-use,ripgrep,ffmpeg
# ============================================================================
set -euo pipefail

# --- env hygiene (hermes lesson: leaking PYTHONPATH breaks pip installs) ---
if [ -n "${PYTHONPATH:-}" ]; then echo "⚠ ignoring inherited PYTHONPATH"; unset PYTHONPATH; fi
if [ -n "${PYTHONHOME:-}" ]; then echo "⚠ ignoring inherited PYTHONHOME"; unset PYTHONHOME; fi
export UV_NO_CONFIG=1

# --- colors / logging ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'; MAGENTA='\033[1;35m'; NC='\033[0m'
log_info()    { echo -e "${CYAN}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

echo -e "\n${MAGENTA}┌──────────────────────────────────────────────┐${NC}"
echo -e "${MAGENTA}│            🌡 Mercury Installer                │${NC}"
echo -e "${MAGENTA}│   Hermes + omp — one agent, one config        │${NC}"
echo -e "${MAGENTA}│   Mercury is a hybrid distribution of         │${NC}"
echo -e "${MAGENTA}│   Hermes by Nous Research and omp by can1357  │${NC}"
echo -e "${MAGENTA}└──────────────────────────────────────────────┘${NC}\n"

# --- config ---
MERCURY_HOME="${MERCURY_HOME:-$HOME/.mercury}"
# MERCURY LAYOUT (hermes pattern, faithful): code + state under one home;
# the COMMAND is a tiny shim in ~/.local/bin — already on PATH by default
# on modern distros, which is why hermes' one-liner needs zero extra steps.
#   code+state -> $MERCURY_HOME (default ~/.mercury; code at mercury-agent/)
#   command    -> $BIN_DIR       (default ~/.local/bin — ON PATH by default)
#   managed bins (uv, browser-use) -> $MERCURY_HOME/bin
INSTALL_ROOT="${MERCURY_INSTALL_ROOT:-$HOME/.mercury/mercury-agent}"
BIN_DIR="${MERCURY_BIN_DIR:-$HOME/.local/bin}"
MANAGED_BIN="$MERCURY_HOME/bin"
TARBALL_URL=""
RUN_SETUP=true
NON_INTERACTIVE=false
SKIP_BROWSER=false
SKIP_COMPUTER_USE=false
SKIP_GATEWAY=false
NO_SKILLS=false
ENSURE_DEPS=""
OS=""; DISTRO=""; ARCH=""
UV_CMD=""

IS_INTERACTIVE=true
[ -t 0 ] || IS_INTERACTIVE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --tarball) TARBALL_URL="$2"; shift 2 ;;
        --dir) INSTALL_ROOT="$2"; shift 2 ;;
        --skip-setup) RUN_SETUP=false; shift ;;
        --non-interactive|--yes|-y) NON_INTERACTIVE=true; RUN_SETUP=false; shift ;;
        --skip-browser|--no-playwright) SKIP_BROWSER=true; shift ;;
        --skip-computer-use) SKIP_COMPUTER_USE=true; shift ;;
        --skip-gateway) SKIP_GATEWAY=true; shift ;;
        --no-skills) NO_SKILLS=true; shift ;;
        --ensure) ENSURE_DEPS="$2"; shift 2 ;;
        -h|--help) sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)
            if [ -z "$TARBALL_URL" ] && { [[ "$1" == http* ]] || [ -f "$1" ]; }; then TARBALL_URL="$1"; shift
            else echo "Unknown option: $1"; exit 1; fi ;;
    esac
done

# --- prompting: works under curl|bash via /dev/tty (hermes pattern) ---
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

# ============================================================================
# system detection
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
        log_warn "musl system — the omp binary links libstdc++/libgcc dynamically:"
        log_warn "  if it fails to start: apk add libstdc++ libgcc"
    fi
}

# ============================================================================
# uv (hermes managed-uv pattern: owns its copy, no PATH dependence)
# ============================================================================
install_uv() {
    local managed="$MERCURY_HOME/bin/uv"
    if [ -x "$managed" ]; then UV_CMD="$managed"; log_success "managed uv found ($("$managed" --version 2>/dev/null)"; return 0; fi
    if command -v uv >/dev/null 2>&1; then UV_CMD="$(command -v uv)"; log_success "uv found on PATH"; return 0; fi
    log_info "Installing managed uv into $MERCURY_HOME/bin ..."
    mkdir -p "$MERCURY_HOME/bin"
    local installer logf
    installer="$(mktemp)"; logf="$(mktemp)"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$installer" 2>"$logf"; then
        log_error "uv download failed"; sed 's/^/    /' "$logf" >&2; exit 1
    fi
    if UV_UNMANAGED_INSTALL="$MERCURY_HOME/bin" sh "$installer" >>"$logf" 2>&1 && [ -x "$managed" ]; then
        UV_CMD="$managed"; log_success "managed uv installed ($("$managed" --version 2>/dev/null))"
    else
        log_error "uv install failed"; sed 's/^/    /' "$logf" >&2; exit 1
    fi
    rm -f "$installer" "$logf"
}

# ============================================================================
# optional system packages (hermes pattern: distro-aware, sudo-aware, best-effort)
# ============================================================================
install_system_packages() {
    local missing=""
    command -v rg  >/dev/null 2>&1 || missing="$missing ripgrep"
    command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
    [ -z "$missing" ] && { log_success "system tools present (rg, ffmpeg)"; return 0; }
    log_info "missing system tools:$missing"
    if [ "$NON_INTERACTIVE" = true ]; then TRY_SYS=true
    else prompt "Install missing system packages (may need sudo)?" yes && TRY_SYS=true || TRY_SYS=false; fi
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

# ============================================================================
# Browser Use CLI (hermes pattern: uv tool into the managed bin — this is the
# browser backend; NO python playwright module is involved)
# ============================================================================
install_browser_use_cli() {
    if [ "$SKIP_BROWSER" = true ]; then log_info "skipping Browser Use CLI (--skip-browser)"; return 0; fi
    [ "$DISTRO" = "termux" ] && return 0
    [ -n "$UV_CMD" ] || { log_info "skipping Browser Use CLI (uv unavailable)"; return 0; }
    if [ -x "$MERCURY_HOME/bin/browser-use" ]; then log_success "Browser Use CLI already installed"; return 0; fi
    log_info "Installing Browser Use CLI (default browser backend)..."
    if run_with_timeout 600 env UV_NO_CONFIG=1 UV_TOOL_BIN_DIR="$MERCURY_HOME/bin" \
        "$UV_CMD" tool install browser-use >/dev/null 2>&1; then
        log_success "Browser Use CLI installed"
    else
        log_warn "Browser Use CLI install failed — browser automation falls back to built-in tools"
        log_info "Install later with: $UV_CMD tool install browser-use  (or 'mercury tools')"
    fi
}

# ============================================================================
# cua-driver (hermes pattern: trycua's upstream installer, time-boxed, log tail)
# ============================================================================
run_with_timeout() { # $1=secs, rest=cmd
    local secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi
}

cua_driver_runtime_compatible() {
    local driver_path version_output major minor
    driver_path="$(command -v cua-driver 2>/dev/null)" || return 1
    version_output="$("$driver_path" --version 2>/dev/null)" || return 1
    [[ "$version_output" =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]] || return 1
    major="${BASH_REMATCH[1]}"; minor="${BASH_REMATCH[2]}"
    (( major == 0 && minor < 20 )) && return 1
    return 0
}

install_computer_use_driver() {
    if [ "$SKIP_COMPUTER_USE" = true ]; then log_info "skipping cua-driver (--skip-computer-use)"; return 0; fi
    [ "$DISTRO" = "termux" ] && return 0
    if command -v cua-driver >/dev/null 2>&1; then
        if cua_driver_runtime_compatible; then log_success "cua-driver already installed and compatible"; return 0; fi
        log_warn "existing cua-driver is old; repairing"
    fi
    if [ "$(uname -s)" = "Darwin" ] && [ -d /Applications ] && [ ! -w /Applications ]; then
        log_info "skipping cua-driver: /Applications not writable"; return 0
    fi
    log_info "Installing Computer Use driver (cua-driver)..."
    local cua_log; cua_log="$(mktemp)"
    if run_with_timeout 660 /bin/bash -c \
        'curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh | /bin/bash' \
        >"$cua_log" 2>&1; then
        log_success "cua-driver installed (enable via 'mercury tools' → Computer Use)"
    else
        log_warn "cua-driver install failed — it installs on demand when you enable the tool"
        log_info "Install later with: mercury computer-use install"
        tail -n 5 "$cua_log" >&2 || true
    fi
    rm -f "$cua_log"
}

# ============================================================================
# source + python
# ============================================================================
fetch_tarball() {
    if [ -n "$TARBALL_URL" ]; then
        # PER-ARCH ASSETS (user directive): the release publishes one tarball
        # per architecture; rewrite the URL so each host downloads exactly
        # the binary it needs (no dead weight in the download). The rewrite
        # is BIDIRECTIONAL (2026-09-06): a pasted -x64 URL on an arm host
        # becomes -arm64 AND a pasted -arm64 URL on an x86 host becomes
        # -x64. One link in the README is genuinely arch-neutral; an
        # emulated (wrong-arch) install can no longer happen by accident.
        _arch="$(uname -m)"
        case "$_arch" in
            aarch64|arm64) _arch=arm64 ;;
            x86_64|amd64)  _arch=x64 ;;
            *) _arch="" ;;
        esac
        if [[ "$TARBALL_URL" == http* ]] && [ -n "$_arch" ]; then
            _norm="$(echo "$TARBALL_URL" | sed -E 's/-(x64|arm64)\.tar\.gz$/.tar.gz/')"
            _want="${_norm%.tar.gz}-$_arch.tar.gz"
            if [ "$_want" != "$TARBALL_URL" ]; then
                # Probe robustly: a plain HEAD (-I) is flaky on some networks/
                # proxies; fall back to a 1-byte ranged GET, which exercises
                # the same redirect chain the real download will use.
                if curl -fsSI --connect-timeout 10 "$_want" >/dev/null 2>&1 \
                   || curl -fsSL --connect-timeout 10 --max-time 20 -r 0-0 -o /dev/null "$_want"; then
                    TARBALL_URL="$_want"
                    log_info "$_arch host — using the $_arch tarball"
                else
                    log_warn "could not confirm the -$_arch asset (network probe failed);"
                    log_warn "retrying with it anyway — download will fail loudly if truly absent"
                    TARBALL_URL="$_want"
                fi
            fi
        fi
        log_info "fetching distribution tarball"
        TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
        if [ -f "$TARBALL_URL" ]; then cp "$TARBALL_URL" "$TMP/mercury.tar.gz"
        else
            curl -fsSL --connect-timeout 10 --speed-limit 1024 --speed-time 60 "$TARBALL_URL" -o "$TMP/mercury.tar.gz" \
                || { log_error "tarball download failed"; exit 1; }
        fi
        local SHA_SRC="" want have
        if [ -f "${TARBALL_URL}.sha256" ]; then SHA_SRC="${TARBALL_URL}.sha256"
        elif [[ "$TARBALL_URL" == http* ]] && curl -fsSL "${TARBALL_URL}.sha256" -o "$TMP/mercury.tar.gz.sha256" 2>/dev/null; then SHA_SRC="$TMP/mercury.tar.gz.sha256"; fi
        if [ -n "$SHA_SRC" ]; then
            want="$(awk '{print $1}' "$SHA_SRC" | head -1)"
            have="$(sha256sum "$TMP/mercury.tar.gz" | awk '{print $1}')"
            [ "$want" = "$have" ] && log_success "checksum verified" \
                || { log_error "CHECKSUM MISMATCH — corrupt download"; exit 1; }
        fi
        log_info "unpacking to $INSTALL_ROOT"
        tar -xzf "$TMP/mercury.tar.gz" -C "$TMP"
        mkdir -p "$INSTALL_ROOT"
        mkdir -p "$INSTALL_ROOT"
        # the venv is NOT in the tarball; rsync --delete would destroy it and
        # force a full dependency reinstall every update. Preserve it.
        if [ -x "$INSTALL_ROOT/hermes/.venv" ]; then
            mv "$INSTALL_ROOT/hermes/.venv" "$TMP/venv-keep"
        fi
        rsync -a --delete "$TMP/mercury/" "$INSTALL_ROOT/" 2>/dev/null || cp -r "$TMP/mercury/." "$INSTALL_ROOT/"
        if [ -d "$TMP/venv-keep" ]; then
            rm -rf "$INSTALL_ROOT/hermes/.venv"
            mv "$TMP/venv-keep" "$INSTALL_ROOT/hermes/.venv"
        fi
    elif [ -x "$INSTALL_ROOT/bin/mercury" ]; then
        log_info "existing tree at $INSTALL_ROOT — reinstalling in place (~/.mercury state kept)"
    else
        log_error "no tarball URL and no existing tree at $INSTALL_ROOT"
        log_info  "usage: install.sh <tarball-url>"; exit 1
    fi
    cd "$INSTALL_ROOT"
    [ -f bin/mercury ] || { log_error "distribution incomplete: bin/mercury missing"; exit 1; }
    select_omp_binary
    [ -f hermes/ui-tui/dist/entry.js ] || { log_error "incomplete: ui-tui bundle missing"; exit 1; }
}

# ============================================================================
# omp binary: arch selection (user directive — arm64 hosts must install too).
# Primary path: per-arch tarballs (fetch already downloaded the right one;
# dist/omp IS the host-arch binary). Legacy path: dual-binary tarballs that
# carried both prebuilts (x86-64 as dist/omp, arm64 as dist/omp-linux-arm64)
# — pick the one matching the host. Either way dist/omp ends up executable
# for THIS host; every downstream surface looks for dist/omp.
# ============================================================================
select_omp_binary() {
    local DIST="$INSTALL_ROOT/omp/packages/coding-agent/dist"
    # ELF e_machine LSB at offset 18 (measured): 62 = EM_X86_64, 183 = EM_AARCH64
    local MAGIC=""
    [ -x "$DIST/omp" ] && MAGIC="$(dd if="$DIST/omp" bs=1 skip=18 count=1 2>/dev/null | od -An -tuC | tr -d ' ')"
    local BIN=""
    case "$(uname -m)" in
        x86_64|amd64)
            # VERIFY the arch — an arm64 tarball on an x86 host used to
            # install "successfully" and die later at exec time.
            if [ -x "$DIST/omp" ] && [ "$MAGIC" = "62" ]; then
                BIN="$DIST/omp"
            elif [ -x "$DIST/omp" ] && [ "$MAGIC" = "183" ]; then
                log_error "WRONG ARCH: this tarball's omp binary is AArch64 but the host is x86_64."
                log_error "Install with the x64 tarball (the installer usually rewrites the URL;"
                log_error "this one was forced past it). Re-run the README one-liner as-is."
                exit 1
            fi ;;
        arm64|aarch64)
            if [ -x "$DIST/omp-linux-arm64" ]; then
                BIN="$DIST/omp-linux-arm64"
                log_info "arm64 host — using the prebuilt aarch64 omp binary"
            elif [ -x "$DIST/omp" ] && [ "$MAGIC" = "183" ]; then
                BIN="$DIST/omp"   # single-arch tarball that happens to be arm64
            elif [ -x "$DIST/omp" ] && [ "$MAGIC" = "62" ]; then
                # THE 100%-CPU KILLER: x86 binary on an arm host runs under
                # software emulation (10-50x CPU, pegs every core). Hard-fail
                # instead of installing an emulated binary.
                log_error "WRONG ARCH: this tarball's omp binary is x86_64 but the host is $(uname -m)."
                log_error "An x86 binary would run EMULATED (10-50x CPU — pegs every core)."
                log_error "Install with the arm64 tarball: re-run the README one-liner as-is"
                log_error "(the installer rewrites the URL for aarch64 hosts automatically)."
                exit 1
            fi ;;
    esac
    if [ -z "$BIN" ]; then
        log_error "no omp binary for $(uname -m) in this tarball"
        log_info  "run from a checkout: cd omp/packages/coding-agent && bun install && bun run build"
        exit 1
    fi
    if [ "$BIN" != "$DIST/omp" ]; then
        ln -f "$BIN" "$DIST/omp" 2>/dev/null || cp -f "$BIN" "$DIST/omp"
    fi
    [ -x "$DIST/omp" ] || { log_error "incomplete: prebuilt omp binary missing"; exit 1; }
}

setup_venv() {
    log_info "python environment (uv + exact-pinned venv)"
    local VENV="$INSTALL_ROOT/hermes/.venv"
    if [ ! -x "$VENV/bin/python" ]; then
        "$UV_CMD" venv "$VENV" --python '>=3.11,<3.14' 2>/dev/null || "$UV_CMD" venv "$VENV"
        ( cd hermes && "$UV_CMD" pip install --python "$VENV/bin/python" -q -e . ) || { log_error "python deps failed"; exit 1; }
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
    ( cd hermes && "$VENV/bin/python" -c "import mercury_cli.main" ) >/dev/null 2>&1 \
        || { log_error "hermes CLI import failed"; exit 1; }
    log_success "hermes CLI imports"
}

# ============================================================================
# THE setup wizard: `mercury setup` — the full hermes wizard (provider OAuth,
# Nous Portal one-shot, model pickers, API keys, TTS, tools, telemetry),
# which now ALSO syncs the omp engine at its tail (mercury_cli/omp_sync).
# Stdin from /dev/tty so it works under curl|bash (hermes pattern).
# ============================================================================
run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then log_info "skipping setup wizard (--skip-setup/--non-interactive)"; return 0; fi
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "setup wizard skipped (no terminal). Run 'mercury setup' after install."
        return 0
    fi
    echo ""
    log_info "starting setup wizard (configures BOTH engines: models, OAuth, tools)..."
    echo ""
    local VENV="$INSTALL_ROOT/hermes/.venv"
    # Env must mirror bin/mercury EXACTLY: HERMES_HOME is the engine-private
    # home (auth store, .env) and PI_CODING_AGENT_DIR the omp one. Without
    # these the wizard writes credentials to the platform default home while
    # the launcher reads $MERCURY_HOME/hermes — the "no provider configured"
    # bug class.
    ( cd hermes \
        && MERCURY_HOME="$MERCURY_HOME" \
        MERCURY_CONFIG="$MERCURY_HOME/config.yaml" \
        MERCURY_SKILLS_DIR="$MERCURY_HOME/skills" \
        HERMES_HOME="$MERCURY_HOME/hermes" \
        PI_CODING_AGENT_DIR="$MERCURY_HOME/omp" \
        PYTHONPATH="$INSTALL_ROOT/hermes" \
        "$VENV/bin/python" -m mercury_cli.main setup </dev/tty ) || log_warn "setup wizard exited non-zero — run 'mercury setup' later"

    # Approval mode (user directive): the installer ASKS. One knob, both
    # engines — omp inherits the hermes agent's mode at spawn (bridge maps
    # approvals.mode -> omp tools.approvalMode; deny rules always carry).
    echo ""
    log_info "Approval mode — how much the agent may do without asking you:"
    echo "  1) safe   — read-only auto-approved; writes & commands ask (default)"
    echo "  2) smart  — reads + workspace writes auto-approved; commands ask"
    echo "  3) yolo   — never ask; full auto (deny rules still enforced)"
    local APPROVAL_CHOICE=""
    while [ -z "$APPROVAL_CHOICE" ]; do
        printf 'Choose [1/2/3, default=1]: '
        read -r APPROVAL_CHOICE </dev/tty || APPROVAL_CHOICE="1"
        case "$APPROVAL_CHOICE" in
            1|""|safe) APPROVAL_CHOICE="manual" ;;
            2|smart)   APPROVAL_CHOICE="smart" ;;
            3|yolo)    APPROVAL_CHOICE="off" ;;
            *) APPROVAL_CHOICE="" ;;
        esac
    done
    "$VENV/bin/python" - <<PYEOF
import os, sys
sys.path.insert(0, "$INSTALL_ROOT/hermes")
from mercury_cli.config import load_config, save_config
cfg = load_config() or {}
cfg["approvals"] = {"mode": "$APPROVAL_CHOICE"}
save_config(cfg)
print("approvals.mode = $APPROVAL_CHOICE (both engines; omp inherits at spawn)")
PYEOF
    # re-render the omp subtree so the child engine picks the mode up now
    ( cd "$INSTALL_ROOT/hermes" \
        && MERCURY_HOME="$MERCURY_HOME" MERCURY_CONFIG="$MERCURY_HOME/config.yaml" \
        HERMES_HOME="$MERCURY_HOME/hermes" PI_CODING_AGENT_DIR="$MERCURY_HOME/omp" \
        PYTHONPATH="$INSTALL_ROOT/hermes" \
        "$VENV/bin/python" -m mercury_cli.main omp-sync --quiet ) || true
}

# ============================================================================
# gateway (hermes pattern: offer background service when messaging tokens exist)
# ============================================================================
maybe_start_gateway() {
    [ "$SKIP_GATEWAY" = true ] && return 0
    local ENV_FILE="$MERCURY_HOME/.env"
    [ -f "$ENV_FILE" ] || return 0
    local HAS_MESSAGING=false VAL VAR
    for VAR in TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN WHATSAPP_ENABLED SIMPLEX_BOT_TOKEN; do
        VAL=$(grep "^${VAR}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        [ -n "$VAL" ] && HAS_MESSAGING=true && break
    done
    [ "$HAS_MESSAGING" = false ] && return 0
    echo ""
    log_info "Messaging platform token detected — the gateway must run for the bot to work."
    if ! (: </dev/tty) 2>/dev/null; then return 0; fi
    prompt "Install the gateway as a background service?" yes || return 0
    local MERCURY_CMD="$BIN_DIR/mercury"
    if command -v systemctl >/dev/null 2>&1 && [ "$DISTRO" != "termux" ]; then
        log_info "installing systemd service..."
        if "$MERCURY_CMD" gateway install >/dev/null 2>&1; then
            log_success "gateway service installed"
            "$MERCURY_CMD" gateway start >/dev/null 2>&1 && log_success "gateway started — your bot is online" \
                || log_warn "service installed but failed to start: mercury gateway start"
        else
            log_warn "systemd install failed: start manually with 'mercury gateway'"
        fi
    else
        nohup "$MERCURY_CMD" gateway > "$MERCURY_HOME/logs/gateway.log" 2>&1 &
        log_success "gateway started (PID $!) — logs: $MERCURY_HOME/logs/gateway.log"
    fi
}

# ============================================================================
# seeds + path
# ============================================================================
seed_defaults() {
    # ONE env (user rule — no legacy paths, no symlinks): if a PREVIOUS
    # Mercury build left its env at the old engine path, adopt it into THE
    # shared file once, visibly, then REMOVE the old file. The code never
    # reads that path again; this is installer-level data placement only.
    local _old_env="$MERCURY_HOME/hermes/.env"
    if [ -f "$_old_env" ] && [ ! -f "$MERCURY_HOME/.env" ]; then
        mv "$_old_env" "$MERCURY_HOME/.env" && log_success "adopted existing API keys into $MERCURY_HOME/.env"
    elif [ -f "$_old_env" ] && [ -f "$MERCURY_HOME/.env" ]; then
        # both exist: merge any keys the shared file lacks, then remove old
        while IFS='=' read -r _k _v; do
            case "$_k" in ''|'#'*) continue ;; esac
            grep -q "^${_k}=" "$MERCURY_HOME/.env" 2>/dev/null || echo "${_k}=${_v}" >> "$MERCURY_HOME/.env"
        done < "$_old_env"
        rm -f "$_old_env" && log_success "merged old engine .env into $MERCURY_HOME/.env and removed it"
    fi
    chmod 600 "$MERCURY_HOME/.env" 2>/dev/null || true
    log_info "seeding hand-editable defaults into $MERCURY_HOME/config/ (never overwrites)"
    mkdir -p "$MERCURY_HOME/config"
    for _seed in HERMES.md OMP.md SOUL.md MEMORY.md USER.md AGENTS.md; do
        # MERCURY LAYOUT: config/ is THE location; migrate stray top-level files once
        [ -f "$MERCURY_HOME/$_seed" ] && [ ! -f "$MERCURY_HOME/config/$_seed" ] && mv "$MERCURY_HOME/$_seed" "$MERCURY_HOME/config/$_seed" 2>/dev/null || true
        [ -f "$MERCURY_HOME/config/$_seed" ] || cp "$INSTALL_ROOT/config/$_seed" "$MERCURY_HOME/config/$_seed" 2>/dev/null || true
    done
    if [ "$NO_SKILLS" = true ]; then
        log_info "--no-skills: seeding no bundled skills"
    elif [ -d "$INSTALL_ROOT/hermes/skills" ]; then
        if [ ! -d "$MERCURY_HOME/skills" ]; then
            log_info "seeding the shared skills library (~/.mercury/skills)"
            mkdir -p "$MERCURY_HOME/skills"
            ( cd "$INSTALL_ROOT/hermes/skills" && tar cf - . ) | ( cd "$MERCURY_HOME/skills" && tar xf - )
        else
            for _cat in "$INSTALL_ROOT/hermes/skills"/*/; do
                _name="$(basename "$_cat")"
                [ -d "$MERCURY_HOME/skills/$_name" ] || cp -r "$_cat" "$MERCURY_HOME/skills/$_name"
            done
        fi
    fi
}

setup_path() {
    mkdir -p "$BIN_DIR"
    # HERMES PATTERN (faithful): a real shim SCRIPT in the default-PATH dir —
    # not a symlink. Clears PYTHONPATH/PYTHONHOME so an inherited env can't
    # shadow the install; rm-first so an old symlink is never followed.
    rm -f "$BIN_DIR/mercury"
    cat > "$BIN_DIR/mercury" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
export MERCURY_HOME="${MERCURY_HOME:-$HOME/.mercury}"
exec "$INSTALL_ROOT/bin/mercury" "\$@"
EOF
    chmod +x "$BIN_DIR/mercury"
    log_success "mercury command → $BIN_DIR/mercury (shim; ~/.local/bin is on PATH by default)"

    # Stale residue from earlier Mercury layouts (NOT the shim above):
    for _stale in "$HOME/.mercury/bin/mercury" "/usr/local/bin/mercury" "/usr/local/bin/mercury-acp" "/usr/local/bin/mercury-agent"; do
        if [ "$_stale" != "$BIN_DIR/mercury" ] && { [ -e "$_stale" ] || [ -L "$_stale" ]; }; then
            rm -f "$_stale" && log_info "removed stale: $_stale"
        fi
    done
    local _stale_tree="$HOME/.local/share/mercury"
    if [ -d "$_stale_tree" ] && [ "$INSTALL_ROOT" != "$_stale_tree" ]; then
        rm -rf "$_stale_tree" && log_info "removed stale install tree: $_stale_tree"
    fi

    ensure_path_configured
}

ensure_path_configured() {
    # HERMES PATTERN (faithful copy): probe a real non-login interactive
    # shell exactly as the user will use it; only if it cannot resolve
    # mercury, write the PATH export into EVERY existing shell rc.
    # Permanent across reboots; covers login, non-login, and new shells.
    if env -i HOME="$HOME" TERM="${TERM:-dumb}" PATH="/usr/local/bin:/usr/bin:/bin" \
        bash -i -c 'command -v mercury' >/dev/null 2>&1; then
        log_success "mercury resolves in interactive shells (PATH already configured)"
        return 0
    fi

    LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
    SHELL_CONFIGS=()
    IS_FISH=false
    case "$LOGIN_SHELL" in
        zsh)
            [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
            [ -f "$HOME/.zprofile" ] && SHELL_CONFIGS+=("$HOME/.zprofile")
            if [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
                touch "$HOME/.zshrc" && SHELL_CONFIGS+=("$HOME/.zshrc")
            fi
            ;;
        fish)
            IS_FISH=true
            FISH_CONFIG="$HOME/.config/fish/config.fish"
            mkdir -p "$(dirname "$FISH_CONFIG")"
            touch "$FISH_CONFIG"
            ;;
        *)
            # bash and everything POSIX: write ALL existing rc files, and
            # create the minimal pair when none exists
            if [ ! -f "$HOME/.bashrc" ] && [ ! -f "$HOME/.bash_profile" ] && [ ! -f "$HOME/.profile" ]; then
                touch "$HOME/.bashrc"
                printf '[ -f ~/.bashrc ] && . ~/.bashrc\n' > "$HOME/.bash_profile"
            fi
            [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
            [ -f "$HOME/.bash_profile" ] && SHELL_CONFIGS+=("$HOME/.bash_profile")
            [ -f "$HOME/.profile" ] && SHELL_CONFIGS+=("$HOME/.profile")
            ;;
    esac

    if [ "$IS_FISH" = true ]; then
        if ! grep -q 'mercury/bin' "$FISH_CONFIG" 2>/dev/null; then
            echo "" >> "$FISH_CONFIG"
            echo "# Mercury — ensure ~/.mercury/bin is on PATH" >> "$FISH_CONFIG"
            echo "fish_add_path -g \"$BIN_DIR\"" >> "$FISH_CONFIG"
            log_success "added $BIN_DIR to PATH ($FISH_CONFIG, fish_add_path)"
        fi
    else
        PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\""
        for SHELL_CONFIG in "${SHELL_CONFIGS[@]}"; do
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null | grep -qF "$BIN_DIR"; then
                echo "" >> "$SHELL_CONFIG"
                echo "# Mercury — ensure $BIN_DIR is on PATH" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "added $BIN_DIR to PATH ($SHELL_CONFIG)"
            fi
        done
    fi

    # system-wide catch-all for login shells (writable without sudo only in
    # containers; best-effort)
    if [ -d /etc/profile.d ] && [ -w /etc/profile.d ] && [ ! -f /etc/profile.d/mercury.sh ]; then
        printf 'export PATH="%s:$PATH"\n' "$BIN_DIR" > /etc/profile.d/mercury.sh 2>/dev/null \
            && log_success "system-wide: /etc/profile.d/mercury.sh"
    fi

    # verify: the SAME probe must now succeed
    if env -i HOME="$HOME" TERM="${TERM:-dumb}" PATH="/usr/local/bin:/usr/bin:/bin" \
        bash -i -c 'command -v mercury' >/dev/null 2>&1; then
        log_success "mercury command ready (verified in a fresh interactive shell)"
    else
        log_warn "PATH written; open a new terminal for it to take effect"
    fi
}

print_success() {
    echo ""
    echo -e "${GREEN}✓ Mercury installed.${NC}"
    echo "  Command:     $BIN_DIR/mercury  (the only mercury executable)"
    echo ""
    echo "  THIS terminal:   source ~/.bashrc      (zsh: ~/.zshrc, fish: config.fish)"
    echo "  New terminals:   automatic (PATH is on disk)"
    echo "  First run:   mercury"
    echo "  omp engine:  mercury omp"
    echo "  Reconfigure: mercury setup   (both engines)"
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
    setup_path            # command link EARLY: a later failure must not
                          # leave an installed-but-unlaunchable system
    install_system_packages
    install_browser_use_cli
    install_computer_use_driver
    seed_defaults
    run_setup_wizard
    maybe_start_gateway
    print_success
}

if [ -n "$ENSURE_DEPS" ]; then
    detect_system
    INSTALL_ROOT="${MERCURY_INSTALL_ROOT:-$HOME/.mercury/mercury-agent}"
    install_uv
    for d in ${ENSURE_DEPS//,/ }; do
        case $d in
            browser) install_browser_use_cli ;;
            computer-use|cua) install_computer_use_driver ;;
            ripgrep|ffmpeg) install_system_packages ;;
            *) log_warn "unknown --ensure dep: $d" ;;
        esac
    done
    exit 0
fi

main
