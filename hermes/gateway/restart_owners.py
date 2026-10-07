"""Restart lifecycle metadata only; native owners retain all task/grant authority."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.async_delegation import process_identity_state
from gateway.status import get_process_start_time


@dataclass(frozen=True)
class _Owner:
    owner_id: str
    home: Path
    token: str
    pid: int
    started_at: int
    active_work: Callable[[], int]
    checkpoint: Callable[[str], Path | str]
    detach: Callable[[], None]
    guard: threading.RLock = field(default_factory=threading.RLock)


_LOCK = threading.RLock()
_OWNERS: dict[tuple[Path, str], _Owner] = {}
_GENERATION = 0
_CHECKPOINT_GENERATION: int | None = None
_DETACHED: set[str] = set()
_INDEX = "restart-owners.json"
_RECEIPT_FIELDS = {
    "owner_id", "registration_token", "coordinator_pid", "coordinator_started_at",
    "gateway_pid", "gateway_started_at", "checkpoint_path", "sha256", "reason",
    "timestamp",
}


def _home(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def register_owner(owner_id: str, *, profile_home: Path | str, pid: int,
                   started_at: int, active_work: Callable[[], int],
                   checkpoint: Callable[[str], Path | str],
                   detach: Callable[[], None]) -> str:
    """Register a real coordinator; replacement makes old tokens powerless."""
    global _GENERATION, _CHECKPOINT_GENERATION
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("owner_id must be nonempty")
    if type(pid) is not int or pid <= 0 or type(started_at) is not int or started_at < 0:
        raise ValueError("coordinator PID and birth must be integers")
    if not all(callable(callback) for callback in (active_work, checkpoint, detach)):
        raise TypeError("owner callbacks must be callable")
    home = _home(profile_home)
    key = (home, owner_id)
    # Retry if a concurrent replacement changed the guard we acquired.
    while True:
        with _LOCK:
            previous = _OWNERS.get(key)
            guard = previous.guard if previous else threading.RLock()
        with guard, _LOCK:
            if _OWNERS.get(key) is not previous:
                continue
            token = uuid.uuid4().hex
            _OWNERS[key] = _Owner(owner_id, home, token, pid, started_at,
                                  active_work, checkpoint, detach, guard)
            _GENERATION += 1
            _CHECKPOINT_GENERATION = None
            return token


def unregister_owner(token: str) -> bool:
    """Remove runtime registration only, never its durable resume receipt."""
    global _GENERATION, _CHECKPOINT_GENERATION
    with _LOCK:
        match = next(((key, owner) for key, owner in _OWNERS.items()
                      if owner.token == token), None)
    if match is None:
        return False
    key, owner = match
    with owner.guard, _LOCK:
        if _OWNERS.get(key) is not owner:
            return False
        del _OWNERS[key]
        _GENERATION += 1
        _CHECKPOINT_GENERATION = None
        return True


def active_work_count() -> int:
    """Sum cached native work, not coordinator presence or idle roster size."""
    with _LOCK:
        owners = tuple(_OWNERS.values())
    total = 0
    for owner in owners:
        if process_identity_state(owner.pid, owner.started_at) == "dead":
            continue
        try:
            count = owner.active_work()
            if type(count) is not int or count < 0:
                raise ValueError("invalid native activity count")
        except Exception:
            count = 1  # unreadable is not evidence of idle
        with _LOCK:
            if (_OWNERS.get((owner.home, owner.owner_id)) is owner
                    and process_identity_state(owner.pid, owner.started_at) != "dead"):
                total += count
    return total


def _check_private(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("restart state must be a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("restart state must be current-user owned and mode 0600")


@contextmanager
def _directory(home: Path, parts=()):
    """Walk relative to the pinned home without following any child symlink."""
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise PermissionError("profile home must belong to the current user")
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=fd)
            os.close(fd)
            fd = child
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError("restart directory must belong to the current user")
        yield fd
    finally:
        os.close(fd)


def _checkpoint_digest(home: Path, supplied: Path | str, *, sync=False) -> tuple[Path, str]:
    path = Path(supplied)
    if not path.is_absolute():
        path = home / path
    # Do not resolve: resolving would hide symlinks before O_NOFOLLOW checks.
    relative = path.relative_to(home)
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ValueError("checkpoint must be a file under the pinned profile home")
    with _directory(home, relative.parts[:-1]) as directory:
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            _check_private(fd)
            with os.fdopen(fd, "rb", closefd=False) as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if sync:
                os.fsync(fd)
                os.fsync(directory)
        finally:
            os.close(fd)
    return path, digest


@contextmanager
def _index_lock(home: Path):
    home.mkdir(parents=True, exist_ok=True)
    with _directory(home) as root:
        try:
            os.mkdir("runtime", 0o700, dir_fd=root)
        except FileExistsError:
            pass
        with _directory(home, ("runtime",)) as directory:
            os.fchmod(directory, 0o700)
            os.fsync(root)
            fd = os.open("restart-owners.lock",
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600, dir_fd=directory)
            try:
                _check_private(fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield directory
            finally:
                os.close(fd)


def _read_index(directory: int) -> list[dict]:
    try:
        fd = os.open(_INDEX, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    except FileNotFoundError:
        return []
    try:
        _check_private(fd)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            data = json.load(handle)
    finally:
        os.close(fd)
    if (not isinstance(data, dict) or set(data) != {"version", "owners"}
            or data["version"] != 1 or not isinstance(data["owners"], list)):
        raise ValueError("invalid restart owner index")
    seen = set()
    for receipt in data["owners"]:
        if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
            raise ValueError("invalid restart owner receipt")
        for name in ("owner_id", "registration_token", "checkpoint_path", "reason", "timestamp"):
            if not isinstance(receipt[name], str) or not receipt[name]:
                raise ValueError("invalid restart owner receipt field")
        for name in ("coordinator_pid", "coordinator_started_at", "gateway_pid", "gateway_started_at"):
            if type(receipt[name]) is not int or receipt[name] < 0:
                raise ValueError("invalid restart process identity")
        digest = receipt["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("invalid restart checkpoint digest")
        if receipt["owner_id"] in seen:
            raise ValueError("duplicate restart owner receipt")
        seen.add(receipt["owner_id"])
    return data["owners"]


def _write_index(directory: int, receipts: list[dict]) -> None:
    temporary = f".restart-owners-{uuid.uuid4().hex}.json"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump({"version": 1, "owners": receipts}, handle, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _INDEX, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def checkpoint_owners(reason: str) -> list[dict]:
    """Durably checkpoint owners outside the registry lock; errors block restart."""
    global _CHECKPOINT_GENERATION
    if not isinstance(reason, str) or not reason:
        raise ValueError("checkpoint reason must be nonempty")
    with _LOCK:
        generation = _GENERATION
        owners = tuple(_OWNERS.values())
        _CHECKPOINT_GENERATION = None
    receipts = []
    gateway_pid = os.getpid()
    gateway_birth = get_process_start_time(gateway_pid)
    if gateway_birth is None:
        raise RuntimeError("cannot establish gateway process identity")
    for owner in owners:
        path, digest = _checkpoint_digest(owner.home, owner.checkpoint(reason), sync=True)
        receipts.append({
            "owner_id": owner.owner_id, "registration_token": owner.token,
            "coordinator_pid": owner.pid, "coordinator_started_at": owner.started_at,
            "gateway_pid": gateway_pid, "gateway_started_at": gateway_birth,
            "checkpoint_path": str(path), "sha256": digest, "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    with _LOCK:
        if _GENERATION != generation:
            raise RuntimeError("owner registry changed during checkpoint barrier")
        homes = dict.fromkeys(owner.home for owner in owners)
        for home in homes:
            additions = [receipt for owner, receipt in zip(owners, receipts) if owner.home == home]
            with _index_lock(home) as directory:
                old = _read_index(directory)
                replaced = {receipt["owner_id"] for receipt in additions}
                _write_index(directory, [receipt for receipt in old
                                         if receipt["owner_id"] not in replaced] + additions)
        if _GENERATION != generation:
            raise RuntimeError("owner registry changed during checkpoint persistence")
        _CHECKPOINT_GENERATION = generation
    return receipts


def pending_checkpoints(profile_home: Path | str) -> list[dict]:
    """Validate private receipts; caller decides reattach versus reconstruction."""
    home = _home(profile_home)
    with _index_lock(home) as directory:
        receipts = _read_index(directory)
        for receipt in receipts:
            path, digest = _checkpoint_digest(home, receipt["checkpoint_path"])
            if not path.is_absolute() or str(path) != receipt["checkpoint_path"] or digest != receipt["sha256"]:
                raise ValueError("restart checkpoint digest/path mismatch")
        return receipts


def detach_owners() -> int:
    """Invoke restart-only detach once per token after the durable barrier."""
    with _LOCK:
        generation = _GENERATION
        owners = tuple(_OWNERS.values())
        if _CHECKPOINT_GENERATION != generation:
            raise RuntimeError("owners lack a matching durable checkpoint barrier")
    count = 0
    for owner in owners:
        with owner.guard:
            with _LOCK:
                if _GENERATION != generation or _OWNERS.get((owner.home, owner.owner_id)) is not owner:
                    raise RuntimeError("owner registry changed during detach")
                if owner.token in _DETACHED:
                    continue
            if process_identity_state(owner.pid, owner.started_at) != "live":
                raise RuntimeError("cannot verify coordinator identity for detach")
            receipts = pending_checkpoints(owner.home)
            if not any(receipt["owner_id"] == owner.owner_id
                       and receipt["registration_token"] == owner.token
                       and receipt["coordinator_pid"] == owner.pid
                       and receipt["coordinator_started_at"] == owner.started_at
                       and receipt["gateway_pid"] == os.getpid()
                       and receipt["gateway_started_at"] == get_process_start_time(os.getpid())
                       for receipt in receipts):
                raise RuntimeError("owner lacks matching durable checkpoint receipt")
            owner.detach()
            with _LOCK:
                if _GENERATION != generation:
                    raise RuntimeError("owner registry changed during detach callback")
                _DETACHED.add(owner.token)
            count += 1
    return count


def complete_owner_resume(owner_id: str, *, profile_home: Path | str, sha256: str) -> bool:
    """CAS-consume only after the native caller has restored its actual state."""
    home = _home(profile_home)
    with _index_lock(home) as directory:
        receipts = _read_index(directory)
        matching = [receipt for receipt in receipts
                    if receipt["owner_id"] == owner_id and receipt["sha256"] == sha256]
        if not matching:
            return False
        _, digest = _checkpoint_digest(home, matching[0]["checkpoint_path"])
        if digest != sha256:
            raise ValueError("restart checkpoint digest mismatch")
        _write_index(directory, [receipt for receipt in receipts if receipt not in matching])
        return True


def discard_checkpoint(owner_id: str, *, profile_home: Path | str) -> bool:
    """Explicit new/exit revocation; native owner deletes/revokes actual grants."""
    home = _home(profile_home)
    with _index_lock(home) as directory:
        receipts = _read_index(directory)
        retained = [receipt for receipt in receipts if receipt["owner_id"] != owner_id]
        if len(retained) == len(receipts):
            return False
        _write_index(directory, retained)
        return True
