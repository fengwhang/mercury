"""Never let observatory tests control the developer's live daemons."""
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_observatory_services(monkeypatch, tmp_path):
    # A temporary MERCURY_HOME does not sandbox systemctl --user: its
    # unqualified unit names still target the developer's live services.
    # Individual service tests can override this stub to inspect calls.
    real_run = subprocess.run

    def run(args, *positional, **kwargs):
        if isinstance(args, (list, tuple)) and args and Path(str(args[0])).name == "systemctl":
            text = kwargs.get("text") or kwargs.get("universal_newlines")
            empty = "" if text else b""
            # No service is active in the sandbox unless the test says so.
            code = 3 if "is-active" in args else 0
            return subprocess.CompletedProcess(args, code, stdout=empty, stderr=empty)
        return real_run(args, *positional, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    # Unit installers use Path.home()/.config, not MERCURY_HOME or XDG.
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
