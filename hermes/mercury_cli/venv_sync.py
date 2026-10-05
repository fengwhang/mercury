"""Post-publication integration for Mercury's existing launcher contract."""
import logging
from pathlib import Path


def publish_launchers(project_root: Path, *, create: bool = True) -> None:
    """Validate the launcher which resolves committed generations at boot.

    Mercury keeps channel-specific shims installed by install.sh. A dependency
    update must never replace them with stock Hermes commands or redirect a
    different installation's public command.
    """
    from pm.package import InstallError

    launcher = project_root.parent / "bin" / "mercury"
    if not launcher.is_file():
        raise InstallError("launchers", f"Mercury launcher missing: {launcher}",
                           "repair this Mercury installation using its installer")


def collect_superseded_generations(project_root: Path) -> None:
    from mercury_cli.runtime_state import collect_generations
    from pm.environments import install_state_dir
    from pm.runtime import collect_runtime_generations

    try:
        removed = collect_generations(project_root) + collect_runtime_generations(
            install_state_dir(project_root) / "pm-runtime")
    except (OSError, ValueError, RuntimeError) as exc:
        logging.getLogger(__name__).warning("dependency cleanup skipped: %s", exc)
        return
    if removed:
        logging.getLogger(__name__).info("collected %d unused dependency generations", len(removed))
