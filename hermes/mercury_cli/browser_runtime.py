"""Read-only Chromium selection for Mercury browser launchers."""
from __future__ import annotations

import os


def chromium_executable(*, allow_override: bool = True) -> str | None:
    """Prefer the user's executable; otherwise use the verified PM selection."""
    override = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH") if allow_override else None
    if override:
        return override
    from pm import installed_package

    installed = installed_package("chromium")
    return str(installed.binary) if installed and installed.binary is not None else None
