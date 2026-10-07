"""Durable restart admission receipts, distinct from acceptance and exit.

Callers MUST persist the request before accepting it and propagate write errors.
This journal records prospective evidence only: neither a signal callback nor a
shared cgroup identifies the historical sender of a restart.
"""
from __future__ import annotations

import json
import os
import stat
import socket
import struct
import threading
import uuid
from datetime import datetime, timezone

from pathlib import Path
from mercury_constants import get_hermes_home
from gateway.status import get_process_start_time

_JOURNAL_NAME = "gateway-restart-requests.jsonl"
_APPEND_LOCK = threading.Lock()


def _append(record: dict) -> dict:
    # Atomic replacement is inappropriate for an append-only journal. Retain
    # the existing UTF-8 -> flush -> fsync convention, without best-effort errors.
    payload = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    home = get_hermes_home()
    directory = home / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with _APPEND_LOCK:
        fd = os.open(directory / _JOURNAL_NAME, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("restart journal must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise PermissionError("restart journal must belong to the current user")
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                fd = -1  # fdopen owns it, including on write/fsync failure
                if os.name == "posix":
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # A first receipt must survive loss of both new directory entries.
            if os.name == "posix":
                for path in (directory, home):
                    dir_fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
        finally:
            if fd != -1:
                os.close(fd)
    return record


def _record(request_id: str, event: str, state: str) -> dict:
    return {
        "event": event,
        "state": state,
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gateway_pid": os.getpid(),
        "gateway_start_time": get_process_start_time(os.getpid()),
    }


def record_restart_request(
    *, source: str, reason: str, automatic: bool, actor: dict | None = None,
    request_id: str | None = None, active_delegations: list[str] | None = None,
) -> dict:
    """Return a receipt only after durable persistence; failure blocks admission."""
    record = _record(request_id or str(uuid.uuid4()), "request", "requested")
    record.update(
        source=source, reason=reason, automatic=automatic,
        actor=_safe_actor(actor),
        active_delegations=list(active_delegations or []),
    )
    return _append(record)



def record_restart_transition(request_id: str, state: str, **safe_fields) -> dict:
    """Durably correlate a lifecycle decision; never overwrite request evidence.

    Fields are server-owned summaries, not raw control parameters or argv.
    Recording acceptance is a separate operation from recording a request.
    """
    if state not in {"accepted", "deferred", "rejected", "stopping", "exit"}:
        raise ValueError(f"unsupported restart transition: {state}")
    allowed = {
        "reason", "source", "automatic", "active_delegations", "exit_code",
        "signal", "phase", "supervisor", "checkpointed_delegations", "active_work",
    }
    if not request_id or safe_fields.keys() - allowed:
        raise ValueError("restart transition requires an ID and safe summary fields")
    record = _record(request_id, "transition", state)
    record.update(safe_fields)
    return _append(record)


def _safe_operation(executable: str | None, argv: list[str]) -> list[str] | None:
    """Recognize command prefixes, never search arbitrary arguments for verbs.

    Only literal operation tokens survive; profile/unit names, option values,
    shell programs, and unrecognized command lines never enter the journal.
    These are observed process operations, NOT proof of restart causation.
    """
    if not argv:
        return None
    args = argv[1:]
    if executable == "systemctl":
        tokens = ["systemctl"]
        if args and args[0] in {"--user", "--system"}:
            tokens.append(args.pop(0))
        if args and args[0] in {"restart", "stop", "start", "reload", "daemon-reload",
                                "try-restart", "reload-or-restart"}:
            return tokens + [args[0]]
        return None
    if executable and executable.startswith("python"):
        if args[:2] == ["-m", "mercury_cli.main"]:
            args = args[2:]
        elif args and Path(args[0]).name in {"mercury", "mercury-nightly", "hermes"}:
            args = args[1:]
        else:
            return None
    elif executable not in {"mercury", "mercury-nightly", "hermes"}:
        return None
    if len(args) >= 2 and args[0] in {"-p", "--profile"}:
        args = args[2:]  # profile value is deliberately never recorded
    if args[:1] == ["update"]:
        return ["mercury", "update"]
    if len(args) < 2 or args[0] != "gateway" or args[1] not in {
        "restart", "stop", "start", "run",
    }:
        return None
    tokens = ["mercury", args[0], args[1]]
    for token in args[2:]:
        if token not in {"--force", "--replace"}:
            break  # do not mistake a following secret value for an option
        if len(tokens) >= 5:
            break
        tokens.append(token)
    return tokens


def _process_ancestry(pid: int, start_time: int | None) -> list[dict]:
    """Bounded Linux snapshots; unreadable/reused processes end the chain."""
    ancestry = []
    seen = set()
    expected = start_time
    for _ in range(8):
        if pid <= 0 or pid in seen or expected is None:
            break
        seen.add(pid)
        proc = Path(f"/proc/{pid}")
        try:
            fields = (proc / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if int(fields[19]) != expected:
                break
            parent = int(fields[1])
            executable = (proc / "exe").readlink().name
            # Read bounded bytes, discard raw argv immediately after recognizing
            # an operation. Never read environ, cwd, shell code, or full paths.
            with (proc / "cmdline").open("rb") as handle:
                raw = handle.read(8193)
            operation = None if len(raw) > 8192 else _safe_operation(
                executable, raw.decode("utf-8", errors="replace").rstrip("\0").split("\0"),
            )
            if get_process_start_time(pid) != expected:
                break
            ancestry.append({
                "pid": pid, "start_time": expected, "executable": executable,
                "operation": operation,
            })
            pid = parent
            expected = get_process_start_time(pid) if pid > 0 else None
        except (OSError, ValueError, IndexError):
            break
    return ancestry


def _safe_actor(actor: dict | None) -> dict:
    """Project server-owned actor/context onto non-secret evidence fields.

    This is data minimization, not authentication: callers must obtain identity
    from the server-side transport and replace any client-supplied actor.
    """
    if actor is None:
        return {"authentication": "unknown"}
    keys = {
        "authentication", "pid", "uid", "gid", "start_time", "identity_status",
        "same_gateway_cgroup", "kind", "source", "platform", "user_id", "chat_id",
        "session_id", "session_key", "delegation_id", "verb", "signal",
    }
    result = {key: value for key, value in actor.items()
              if key in keys and isinstance(value, (str, int, float, bool, type(None)))}
    result.setdefault("authentication", "unknown")
    context_keys = {
        "source", "platform", "user_id", "chat_id", "session_id", "session_key",
        "delegation_id", "verb", "signal", "supervisor", "phase",
    }
    if isinstance(actor.get("context"), dict):
        result["context"] = {
            key: value for key, value in actor["context"].items()
            if key in context_keys and isinstance(value, (str, int, bool, type(None)))
        }
    if isinstance(actor.get("ancestry"), list):
        result["ancestry"] = []
        for item in actor["ancestry"][:8]:
            if not isinstance(item, dict):
                continue
            operation = item.get("operation")
            if not (isinstance(operation, list) and operation and
                    all(isinstance(token, str) for token in operation) and
                    _safe_operation(operation[0], operation) == operation):
                operation = None
            result["ancestry"].append({
                "pid": item.get("pid") if isinstance(item.get("pid"), int) else None,
                "start_time": item.get("start_time") if isinstance(item.get("start_time"), int) else None,
                "executable": Path(item["executable"]).name if isinstance(item.get("executable"), str) else None,
                "operation": operation,
            })
    return result


def _same_gateway_cgroup(pid: int, start_time: int | None) -> bool | None:
    """Compare verified Linux process membership, never retain cgroup paths."""
    if start_time is None or get_process_start_time(pid) != start_time:
        return None
    try:
        peer = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").strip()
        gateway = Path(f"/proc/{os.getpid()}/cgroup").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not peer or not gateway or get_process_start_time(pid) != start_time:
        return None
    return peer == gateway


def authenticated_control_actor(sock) -> dict:
    """Read server-side transport credentials, never request-provided identity.

    ``sock`` may be asyncio's TransportSocket wrapper. On transports without
    peer credentials, the existing private socket/pipe ACL is the boundary;
    no PID/UID is claimed. A failure on a supported transport is unknown, not
    fabricated authentication. Signal callbacks must not use this function.
    """
    actor = {
        "authentication": "filesystem_acl", "pid": None, "uid": None,
        "gid": None, "start_time": None, "ancestry": [], "same_gateway_cgroup": None,
    }
    if not hasattr(socket, "SO_PEERCRED"):
        actor["identity_status"] = "peer_credentials_unavailable"
        return actor
    try:
        credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", credentials)
        if pid <= 0 or uid < 0 or gid < 0:
            raise ValueError("invalid peer credentials")
    except (AttributeError, OSError, ValueError, struct.error):
        actor.update(authentication="unknown", identity_status="peer_credentials_failed")
        return actor
    start_time = get_process_start_time(pid)
    actor.update(
        authentication="unix_peer_credentials", pid=pid, uid=uid, gid=gid,
        start_time=start_time, ancestry=_process_ancestry(pid, start_time),
        same_gateway_cgroup=_same_gateway_cgroup(pid, start_time),
        identity_status="observed" if start_time is not None else "process_start_unavailable",
    )
    return actor