"""Keep Mercury's application interpreter separate from PM's private runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tomllib

from pm.package import InstallError


def project_python(project: Path, requested: Path | None = None) -> Path | None:
    """Mercury projects use the application Python; other build inputs keep PM defaults."""
    if requested is not None:
        return Path(requested)
    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    if metadata.get("project", {}).get("name") == "hermes-agent":
        return application_python(project)[0]
    return None


def application_python(project: Path) -> tuple[Path, str]:
    """Use the installed application's supported base Python, without downloading.

    PM's pinned 3.14 interpreter runs only the dependency manager. Mercury's
    installer provides an application interpreter matching its own metadata.
    Resolve the base executable so new generations do not depend on old venvs.
    """
    from packaging.specifiers import SpecifierSet

    executable = Path(os.environ.get("MERCURY_PYTHON") or
                      project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    try:
        result = subprocess.run(
            [str(executable), "-I", "-c",
             "import json,sys; print(json.dumps([sys._base_executable, '.'.join(map(str,sys.version_info[:3]))]))"],
            capture_output=True, text=True, timeout=10, check=True)
        base, version = json.loads(result.stdout)
        requirement = tomllib.loads((project / "pyproject.toml").read_text())["project"]["requires-python"]
        if version not in SpecifierSet(requirement) or not Path(base).is_file():
            raise ValueError(f"Python {version} does not satisfy {requirement}")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        raise InstallError("venv", f"Mercury application Python is unavailable or unsupported: {executable}",
                           "run the Mercury installer, or set MERCURY_PYTHON to its supported Python") from exc
    return Path(base).absolute(), version
