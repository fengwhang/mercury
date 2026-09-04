#!/usr/bin/env bash
# Fresh-VM interpreter simulation: uv venv from vendored pyproject, scratch
# prefix, /opt/hermes/.venv completely absent from resolution. Then import
# the critical module set through it.
set -euo pipefail
REPO=/opt/data/home/Documents/mercury
SIM=/tmp/mercury-vm-sim
rm -rf "$SIM"; mkdir -p "$SIM"
export UV_PYTHON_INSTALL_DIR="$SIM/uv-python"

echo "== uv venv (python pinned by pyproject >=3.11,<3.14)"
command -v uv >/dev/null || { echo "uv missing"; exit 1; }
cd "$REPO/hermes"
uv venv "$SIM/venv"
echo "== uv sync (exact-pinned deps from vendored pyproject)"
uv pip install --python "$SIM/venv/bin/python" -e . 2>&1 | tail -3

echo "== critical imports through the fresh venv (MERCURY env, no /opt/hermes)"
cd "$REPO/hermes"
export MERCURY_HOME="$SIM/home/.mercury"
export MERCURY_CONFIG="$MERCURY_HOME/config.yaml"
mkdir -p "$MERCURY_HOME"
PYTHONPATH="$REPO/hermes" "$SIM/venv/bin/python" - <<'PYEOF'
import importlib
mods = ['gateway.run', 'cli', 'run_agent', 'tools.omp_delegation', 'tools.approval',
        'cron.scheduler', 'mercury_cli.main', 'mercury_constants', 'agent.conversation_loop',
        'plugins.web.omp_bridge.provider']
for m in mods:
    importlib.import_module(m)
print(f'VERIFIED: {len(mods)}/{len(mods)} critical imports on a fresh uv venv')
PYEOF
