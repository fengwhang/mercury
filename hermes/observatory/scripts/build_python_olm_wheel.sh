#!/usr/bin/env bash
# Build a CPython-3.13 (cp313) manylinux wheel for python-olm via podman.
#
# WHY THIS EXISTS (2026-09-08, M4c e2ee-real):
#   mautrix 0.21.1 hard-imports the `olm` extension (python-olm) in 6 modules
#   (account.py, sessions.py, decrypt_olm.py, decrypt_megolm.py, signature.py,
#   cross_signing_key.py) — there is no vodozemac-bindings path. python-olm
#   3.2.16 publishes wheels only for cp310–cp312, and the observatory venv is
#   Python 3.13, so the wheel must be built from the sdist. The sdist bundles
#   the complete libolm C++ sources (libolm/), built by its cffi extension
#   script via cmake (fallback: GNU make) — needs g++, make, cmake only.
#   This host has gcc but not g++/olm-devel and no sudo, so the build runs in
#   a throwaway python:3.13-slim container where we ARE root.
#
# USAGE:
#   observatory/scripts/build_python_olm_wheel.sh [venv-dir]
#     - builds the wheel into observatory/scripts/dist/
#     - if a venv dir is passed (or .venv exists next to pyproject.toml),
#       installs the freshly built wheel into it via uv
#
# OUTPUT / LOG:
#   observatory/scripts/dist/python_olm-<ver>-cp313-cp313-linux_*.whl
#   observatory/scripts/logs/wheel-build-<timestamp>.log
#
# REPRODUCIBILITY: env knobs PYTHON_OLM_VERSION / IMAGE / CONTAINER_RUNTIME
# pin every moving part; the log records the exact container commands.
set -euo pipefail

PYTHON_OLM_VERSION="${PYTHON_OLM_VERSION:-3.2.16}"
IMAGE="${IMAGE:-docker.io/library/python:3.13-slim}"
RUNTIME="${CONTAINER_RUNTIME:-podman}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="${DIST_DIR:-$HERE/dist}"
LOG_DIR="$HERE/logs"
mkdir -p "$DIST_DIR" "$LOG_DIR"
LOG="$LOG_DIR/wheel-build-$(date +%Y%m%d-%H%M%S).log"
# NOTE: run as container root — in ROOTLESS podman, container UID 0 maps to
# the invoking host user, so files written to /out land host-owned anyway
# (no --user flag: it would break apt-get inside the container).
"$RUNTIME" run --rm \
    -v "$DIST_DIR":/out:Z \
    "$IMAGE" bash -exc '
        set -euo pipefail
        echo "== container: $(python3 -V) on $(. /etc/os-release && echo "$PRETTY_NAME")"
        apt-get update -qq
        # libffi-dev: cffi sdist fallback (isolation may resolve the sdist);
        # --prefer-binary pins the cffi wheel when one matches.
        apt-get install -y -qq --no-install-recommends g++ make cmake pkg-config libffi-dev
        python3 -m pip install --no-cache-dir --prefer-binary --quiet \
            pip setuptools wheel cffi
        mkdir /src && cd /src
        # --no-binary: force the sdist (there IS no cp313 wheel — that is the
        # point). pip download keeps the PyPI dash filename; normalize it.
        pip download --no-deps --no-binary :all: "python-olm=='$PYTHON_OLM_VERSION'"
        SDIST="$(ls python*olm*.tar.gz | head -n1)"
        # The cffi build script compiles the bundled libolm/ (cmake, make fallback)
        # --no-build-isolation: reuse the cffi installed above (build isolation
        # once re-resolved cffi from sdist and failed without ffi.h).
        pip wheel --no-deps --no-build-isolation -v -w /out "./$SDIST"
        echo "== built:"; ls -la /out
    ' 2>&1 | tee "$LOG"

echo
echo "wheel build log: $LOG"
ls -l "$DIST_DIR"

# Optional install into a uv-managed venv (default: hermes/.venv if present).
VENV="${1:-}"
if [[ -z "$VENV" ]]; then
    for cand in "$HERE/../../.venv" "$HERE/../../../.venv"; do
        [[ -x "$cand/bin/python" ]] && VENV="$cand" && break
    done
fi
if [[ -n "$VENV" && -x "$VENV/bin/python" ]]; then
    WHEEL="$(ls "$DIST_DIR"/python_olm-"$PYTHON_OLM_VERSION"-cp313-*.whl | head -n1)"
    echo "installing $WHEEL into $VENV"
    # mautrix[encryption] extra deps (unpaddedbase64/base58/pycryptodome) —
    # python-olm alone is not enough for mautrix.crypto to import.
    uv pip install --python "$VENV/bin/python" aiosqlite==0.22.1 \
        "mautrix[encryption]==0.21.1" "$WHEEL"
else
    echo "no venv found — wheel left in $DIST_DIR"
fi
