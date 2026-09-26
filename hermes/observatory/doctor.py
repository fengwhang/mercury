"""Observatory doctor: check every link between user and agent.

Answers "why is the bot missing / silent" locally, without guessing:
provisioning, listener liveness, adapter target vs live bind, bot
credential match (lengths only, never secrets), gateway process, and —
decisively — whether the bot nick is actually in the gateway room.
"""
from __future__ import annotations

import os
import select
import socket
import time
from pathlib import Path


def _home(path=None) -> Path:
    from observatory.provision import _mercury_home

    return _mercury_home(path)


def _read_dotenv(home: Path) -> dict:
    out: dict[str, str] = {}
    try:
        for line in (home / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def _env(key: str, dotenv: dict) -> str:
    return os.environ.get(key, "").strip() or dotenv.get(key, "").strip()


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


class _Probe:
    """Minimal sync IRC client: register, JOIN, NAMES, quit."""

    def __init__(self, host: str, port: int, nick: str, password: str):
        self.host = host
        self.port = int(port)
        self.nick = nick
        self.password = password
        self.sock: socket.socket | None = None
        self.buf = b""

    def connect(self, timeout: float = 5.0) -> bool:
        try:
            self.sock = socket.create_connection(
                (self.host, self.port), timeout=timeout)
            self.sock.settimeout(timeout)
        except OSError:
            return False
        try:
            if self.password:
                self._send(f"PASS {self.password}")
            self._send(f"NICK {self.nick}")
            self._send(f"USER {self.nick} 0 * :doctor")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for line in self._drain():
                    if " 001 " in line:
                        return True
                    if " 464 " in line.split():
                        return False
        except OSError:
            return False
        return False

    def _send(self, line: str) -> None:
        assert self.sock is not None
        self.sock.sendall((line + "\r\n").encode())

    def _drain(self) -> list[str]:
        assert self.sock is not None
        try:
            ready, _, _ = select.select([self.sock], [], [], 0.5)
        except (OSError, ValueError):
            return []
        if not ready:
            return []
        try:
            data = self.sock.recv(4096)
        except OSError:
            return []
        if not data:
            return []
        self.buf += data
        lines = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))
        return lines

    def names(self, channel: str, timeout: float = 5.0) -> list[str] | None:
        """Members of *channel*, or None when the join fails."""
        try:
            self._send(f"JOIN {channel}")
            deadline = time.monotonic() + timeout
            members: list[str] = []
            while time.monotonic() < deadline:
                for line in self._drain():
                    parts = line.split()
                    if len(parts) >= 7 and parts[1] == "353":
                        # :srv 353 me = #chan :n1 n2 ...
                        members.extend(
                            n.lstrip(":").lstrip("@+%") for n in parts[6:])
                    if len(parts) >= 4 and parts[1] == "366":
                        return members
                    if len(parts) >= 4 and parts[1] in ("403", "471", "474", "475"):
                        return None
        except OSError:
            return None
        return members

    def close(self) -> None:
        try:
            if self.sock is not None:
                try:
                    self._send("QUIT :doctor")
                except OSError:
                    pass
                self.sock.close()
        except OSError:
            pass
        finally:
            self.sock = None


def run_doctor(home=None) -> list[tuple[bool, str, str]]:
    """Run every check. Returns [(ok, label, detail)]. Secrets never leave."""
    from observatory.provision import read_config, read_irc_passwords

    results: list[tuple[bool, str, str]] = []
    mercury_home = _home(home)
    cfg = read_config(mercury_home) or {}
    server_name = str(cfg.get("server_name") or "mercury")
    agent_host = str(cfg.get("agent_host") or "127.0.0.1")
    agent_port = int(cfg.get("agent_port") or 6669)
    server_host = str(cfg.get("server_host") or "127.0.0.1")
    server_port = int(cfg.get("server_port") or 6670)
    gateway_channel = f"#{server_name}_gateway"
    bot_nick = f"{server_name}_gateway"

    pw = read_irc_passwords(mercury_home) or {}
    agent_pw = str(pw.get("agent") or "")
    server_pw = str(pw.get("server") or "")
    if not agent_pw:
        results.append((False, "passwords",
                        "no agent password provisioned — run setup observatory"))
    else:
        results.append((True, "passwords", "agent + server passwords provisioned"))

    agent_up = _tcp_ok(agent_host, agent_port)
    results.append((agent_up, "agent listener",
                    f"{agent_host}:{agent_port} "
                    + ("answers" if agent_up else "NOTHING LISTENING — is the gateway/ircd up?")))
    server_up = _tcp_ok(server_host, server_port)
    results.append((server_up, "server listener",
                    f"{server_host}:{server_port} "
                    + ("answers" if server_up else "NOTHING LISTENING — clients cannot connect")))

    dotenv = _read_dotenv(mercury_home)
    wired_host = _env("IRC_SERVER", dotenv) or "127.0.0.1"
    wired_port = _env("IRC_PORT", dotenv) or "6669"
    if wired_host == agent_host and wired_port == str(agent_port):
        results.append((True, "adapter target",
                        f"bot dials {wired_host}:{wired_port} (matches live agent bind)"))
    else:
        results.append((False, "adapter target",
                        f"bot dials {wired_host}:{wired_port} but the agent listener "
                        f"is {agent_host}:{agent_port} — re-run setup observatory "
                        f"to re-wire (classic after a bind change)"))

    saved = _env("IRC_SERVER_PASSWORD", dotenv)
    if not agent_pw:
        pass
    elif not saved:
        results.append((False, "bot credential",
                        "IRC_SERVER_PASSWORD not saved — re-run setup observatory"))
    elif saved == agent_pw:
        results.append((True, "bot credential", "matches the agent password"))
    else:
        results.append((False, "bot credential",
                        f"saved copy differs from the agent password "
                        f"(lengths {len(saved)} vs {len(agent_pw)}) — "
                        f"re-run setup observatory to re-wire"))

    try:
        from gateway.status import get_running_pid

        pid = get_running_pid(cleanup_stale=False)
        if pid:
            results.append((True, "gateway process", f"PID {pid}"))
        else:
            results.append((False, "gateway process",
                            "no live gateway PID — start it: <cmd> gateway start"))
    except Exception:
        results.append((False, "gateway process", "could not check (status module unavailable)"))

    if agent_up and agent_pw:
        probe = _Probe(agent_host, agent_port,
                       f"mercury-doctor-{os.getpid() % 10000}", agent_pw)
        try:
            if not probe.connect():
                results.append((False, "bot registration",
                                "probe could not register on the agent listener "
                                "(wrong agent password?)"))
            else:
                members = probe.names(gateway_channel)
                if members is None:
                    results.append((False, "gateway room",
                                    f"could not JOIN {gateway_channel}"))
                elif bot_nick.lower() in {m.lower() for m in members}:
                    results.append((True, "bot in room",
                                    f"{bot_nick} present with "
                                    f"{len(members)} member(s)"))
                else:
                    results.append((False, "bot in room",
                                    f"{bot_nick} NOT in {gateway_channel} "
                                    f"(members: {', '.join(members) or 'none'}) — "
                                    f"the bot is down or on the wrong server"))
        finally:
            probe.close()
    return results
