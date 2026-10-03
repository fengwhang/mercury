"""Mnemosyne storage shared by the engines within one Mercury profile."""
import os
from pathlib import Path

from mercury_constants import get_hermes_home, named_profile_home


def memory_config_path(home: str | Path = "") -> Path:
    active = Path(home) if home else get_hermes_home()
    profile = named_profile_home(active)
    if profile is not None:
        return profile / "config.yaml"
    explicit = os.environ.get("MERCURY_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    root = os.environ.get("MERCURY_HOME", "").strip()
    return Path(root) / "config.yaml" if root else active / "config.yaml"


def memory_home(home: str | Path = "", config_path: str | Path = "") -> Path:
    active = Path(home) if home else get_hermes_home()
    profile = named_profile_home(Path(config_path).parent if config_path else active)
    if profile is not None:
        return profile
    root = os.environ.get("MERCURY_HOME", "").strip()
    if root:
        return Path(root)
    return active.parent if active.name == "hermes" else active


def profile_bank_path(
    configured: str = "", *, home: str | Path = "", config_path: str | Path = ""
) -> str:
    root = memory_home(home, config_path)
    default = root / "memories" / "mnemopi.db"
    if not configured.strip():
        return str(default)
    candidate = Path(configured.strip()).expanduser()
    if named_profile_home(root) is not None:
        if not candidate.is_absolute():
            candidate = root / candidate
        # A copied main/other-profile pin must never join their memory bank.
        if not candidate.resolve().is_relative_to(root.resolve()):
            return str(default)
    return str(candidate)


def ensure_profile_memory(
    profile_dir: Path, *, source_dir: Path | None = None, copy_state: bool = False
) -> None:
    """Rebase copied config pins; full clones snapshot SQLite, including WAL."""
    import shutil
    import sqlite3
    import tempfile
    from copy import deepcopy
    from contextlib import closing
    import yaml

    config_path = profile_dir / "config.yaml"
    if not config_path.exists():
        return
    text = config_path.read_text(encoding="utf-8")
    if config_path.is_symlink():
        config_path.unlink()
        config_path.write_text(text, encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    original = deepcopy(raw)
    hermes = raw.get("hermes", {}) if "hermes" in raw or "omp" in raw else raw
    memory = hermes.get("memory", {})
    native = raw.get("omp", {})
    mn = native.get("mnemopi", {})
    old_db = (
        memory.get("mnemosyne", {}).get("db_path")
        or memory.get("mnemosyne", {}).get("dbPath")
        or mn.get("dbPath", "")
    )
    # A full clone can contain symlinked memory folders or bank files.
    memories = profile_dir / "memories"
    if memories.is_symlink():
        linked_memories = memories.resolve()
        memories.unlink()
        shutil.copytree(linked_memories, memories, symlinks=False)
    memories.mkdir(parents=True, exist_ok=True)
    local_db = old_db
    if old_db and source_dir is not None:
        old_path = Path(old_db).expanduser()
        if old_path.is_absolute() and old_path.is_relative_to(source_dir):
            local_db = str(profile_dir / old_path.relative_to(source_dir))
    destination = Path(profile_bank_path(local_db, home=profile_dir))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if copy_state and source_dir is not None:
        source = Path(profile_bank_path(old_db, home=source_dir))
        if source.is_file():
            fd, temp = tempfile.mkstemp(prefix=".mnemosyne-clone-", dir=memories)
            os.close(fd)
            try:
                with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
                    with closing(sqlite3.connect(temp)) as writer:
                        reader.backup(writer)
                for suffix in ("-wal", "-shm"):
                    Path(str(destination) + suffix).unlink(missing_ok=True)
                os.replace(temp, destination)
            finally:
                Path(temp).unlink(missing_ok=True)
    elif destination.is_symlink():
        # Imports/config clones must not leave an alias to another bank.
        destination.unlink()
    if memory.get("provider") == "mnemosyne" or "mnemosyne" in memory:
        values = memory.setdefault("mnemosyne", {})
        values.pop("dbPath", None)
        values["db_path"] = str(destination)
    if native.get("memory", {}).get("backend") in ("mnemosyne", "mnemopi") or mn:
        native.setdefault("mnemopi", {})["dbPath"] = str(destination)
    if raw != original:
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
