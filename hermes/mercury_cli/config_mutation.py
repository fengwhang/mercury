"""Serialize connector and dashboard config mutations with the same lock."""
from contextlib import contextmanager
import os
import stat
import tempfile
import threading

CONFIG_MUTATION_LOCK = threading.RLock()


@contextmanager
def connector_config_transaction(*, env_keys=()):
    """Roll back configuration and credentials if a card commit fails.

    Keep the dashboard's process lock across both files. Resolve paths under
    the caller's bound profile; never restore another profile's configuration.
    """
    from mercury_cli.config import get_config_path, get_env_path, invalidate_env_cache, is_managed
    from mercury_cli.managed_scope import is_env_managed

    with CONFIG_MUTATION_LOCK:
        if is_managed():
            raise PermissionError("This Mercury installation is managed; connector settings cannot be changed here.")
        blocked = [key for key in env_keys if is_env_managed(key)]
        if blocked:
            raise PermissionError(f"Credentials are managed by your administrator: {', '.join(blocked)}")
        paths = {get_config_path(), get_env_path()}
        snapshots = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                     if path.exists() else None for path in paths}
        environment = {key: os.environ.get(key) for key in env_keys}
        try:
            yield
        except BaseException:
            for path, snapshot in snapshots.items():
                if snapshot is None:
                    path.unlink(missing_ok=True)
                    continue
                data, mode = snapshot
                fd, temporary = tempfile.mkstemp(dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, mode)
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            # save_env_value publishes each successful secret to the process.
            # Restore only changed keys; do not reset unrelated process state.
            for key, value in environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            invalidate_env_cache()
            raise


@contextmanager
def config_write_scope(profile=None):
    if not profile:
        with CONFIG_MUTATION_LOCK:
            yield
        return
    from mercury_cli.web_server import _config_profile_scope

    with _config_profile_scope(profile):
        with CONFIG_MUTATION_LOCK:
            yield
