#!/usr/bin/env bash
# bump-version.sh — stamp ONE version everywhere (user directive:
# every binary reports the Mercury release, no sub-versions).
#
# Usage: bash scripts/bump-version.sh 0.1.33
#
# Stamps:
#   hermes/mercury_cli/__init__.py  (__version__ — CLI, omp bake, MIRC 004)
#   third_party/mlounge/package.json (version + mercuryFork marker —
#     the fork IS the release; refresh_mlounge_fork keys reinstalls off it)
#   omp/packages/utils/mercury-release.json (source-mode product identity)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VER="${1:?usage: bump-version.sh <version, e.g. 0.1.33>}"
case "$VER" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) echo "FATAL: version must look like 0.1.33 (got '$VER')" >&2; exit 1 ;;
esac

REPO="$REPO" VER="$VER" python3 - <<'EOF'
import json
import os
import re
from pathlib import Path

repo = Path(os.environ["REPO"])
ver = os.environ["VER"]

init = repo / "hermes" / "mercury_cli" / "__init__.py"
text = init.read_text(encoding="utf-8")
new, n = re.subn(r'__version__ = "[^"]+"', f'__version__ = "{ver}"', text, count=1)
assert n == 1, "no __version__ line found"
init.write_text(new, encoding="utf-8")

# Preserve key order (no reshuffle diffs); marker rides last.
pkg_path = repo / "third_party" / "mlounge" / "package.json"
pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
pkg["version"] = ver
pkg["mercuryFork"] = True
pkg_path.write_text(
    json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
release_path = repo / "omp" / "packages" / "utils" / "mercury-release.json"
release_path.write_text(json.dumps({"version": ver}, indent="\t") + "\n", encoding="utf-8")
lock_path = pkg_path.with_name("package-lock.json")
if lock_path.is_file():
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["version"] = ver
    root_package = lock.get("packages", {}).get("")
    if isinstance(root_package, dict):
        root_package["version"] = ver
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
print(f"stamped {ver}: Mercury CLI, source OMP, and mLounge package metadata")
EOF
