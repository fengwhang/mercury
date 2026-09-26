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


def _established_to(port: int) -> int:
    """Count ESTABLISHED TCP connections touching *port* (either side).

    Reads /proc/net/tcp directly: no ss dependency, no auth, works for
    any local client including the gateway bot. Returns 0 when unreadable.
    """
    count = 0
    want = f"{int(port):04X}"
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as fh:
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "01":
                        continue
                    local, remote = parts[1], parts[2]
                    if local.rsplit(":", 1)[-1].upper() == want:
                        count += 1
                    elif remote.rsplit(":", 1)[-1].upper() == want:
                        count += 1
        except OSError:
            continue
def _gateway_log_snippets(home: Path) -> list[str]:
    """Recent gateway log lines mentioning irc (any source, any case)."""
    import re as _re

    want = _re.compile(r"\birc\b", _re.IGNORECASE)
    out: list[str] = []
    try:
        import subprocess as _sp

        for pattern in ("IRC", "irc"):
            proc = _sp.run(
                ["journalctl", "--user", "--since", "2 hours ago",
                 "--no-pager", "-q", "--grep", pattern],
                capture_output=True, text=True, timeout=15)
            if proc.returncode == 0 and proc.stdout.strip():
                out.extend(
                    ln for ln in proc.stdout.strip().splitlines()[-60:]
                    if want.search(ln))
    except Exception:
        pass
    try:
        logf = home / "logs" / "gateway.log"
        if logf.is_file():
            tails = logf.read_text(
                encoding="utf-8", errors="replace").splitlines()[-1000:]
            out.extend(
                ln for ln in tails if want.search(ln))
    except OSError:
        pass
    return out[-30:]


def _gateway_proc_home(pid: int) -> str | None:
    """MERCURY_HOME the live gateway process actually runs with."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        for item in raw:
            if item.startswith(b"MERCURY_HOME="):
                return item.split(b"=", 1)[1].decode(
                    "utf-8", errors="replace")
    except OSError:
        pass
    return None


def _gateway_candidate_pids() -> list[int]:
    """PIDs whose cmdline looks like a running mercury gateway."""
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parts = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        text = b" ".join(parts).decode("utf-8", errors="replace")
        if "mercury_cli" in text and "gateway" in text and " run" in text:
            found.append(int(entry.name))
    return sorted(found)


def _proc_uptime(pid: int) -> str | None:
    """Human uptime of *pid* via ps (None when unreadable)."""
    try:
        import subprocess as _sp

        proc = _sp.run(["ps", "-p", str(pid), "-o", "etime="],
                       capture_output=True, text=True, timeout=10)
        out = (proc.stdout or "").strip()
        return out or None
    except Exception:
        return None


def _daemon_unit_state() -> tuple[str | None, str | None, str | None]:
    """(unit, active-since, restarts) for the observatory daemon unit."""
    try:
        import subprocess as _sp

        units = _sp.run(
            ["systemctl", "--user", "list-units", "mercury-*",
             "--all", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=15)
        unit = None
        fallback = None
        for line in (units.stdout or "").splitlines():
            name = line.split()[0] if line.split() else ""
            if not (name.endswith(".service") and "observatory" in name):
                continue
            if "homeserver" in name or "matrix" in name or "tuwunel" in name:
                continue
            if name == "mercury-observatory.service":
                unit = name
                break
            fallback = fallback or name
        if unit is None:
            unit = fallback
        if unit is None:
            return None, None, None
        show = _sp.run(
            ["systemctl", "--user", "show", unit, "-p",
             "ActiveEnterTimestamp", "-p", "NRestarts"],
            capture_output=True, text=True, timeout=15)
        # Named Key=Value output: --value ordering is NOT stable across
        # systemd versions (older ones swapped the two and the row read
        # "active since 0").
        since = restarts = ""
        for line in (show.stdout or "").splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key.strip() == "ActiveEnterTimestamp":
                since = value.strip()
            elif key.strip() == "NRestarts":
                restarts = value.strip()
        return unit, since or None, restarts or None
    except Exception:
        return None, None, None


def _daemon_crash_lines(unit: str) -> list[str]:
    """Recent error/exit lines for the daemon unit (no secrets)."""
    try:
        import subprocess as _sp

        proc = _sp.run(
            ["journalctl", "--user", "-u", unit, "--since", "2 hours ago",
             "--no-pager", "-q", "--grep",
             "[Tt]raceback|OOM|[Kk]illed process|SIGSEGV|SIGABRT"
             "|FAILURE|status=[1-9]"],
            capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip().splitlines()[-10:]
    except Exception:
        pass
    return []

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
    unit, since, restarts = _daemon_unit_state()
    if unit is None:
        results.append((False, "daemon stability",
                        "no observatory daemon unit found — is it installed?"))
    else:
        crash = _daemon_crash_lines(unit)
        detail = f"{unit} active since {since or 'unknown'} ({restarts or '0'} restarts)"
        if crash:
            detail += f"; latest error: {crash[-1][-160:]}"
        bad = bool(crash) or (restarts not in (None, "", "0"))
        if bad:
            detail += " — the daemon is dying under the bot; fix this first"
        results.append((not bad, "daemon stability", detail))
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
            uptime = _proc_uptime(pid)
            results.append((True, "gateway process",
                            f"PID {pid}" + (f", up {uptime}" if uptime else "")))
            dups = [p for p in _gateway_candidate_pids() if p != pid]
            if dups:
                results.append((False, "duplicate gateway",
                                f"extra gateway process(es) {dups} fight yours "
                                f"for the bot nick — kill them, keep {pid}"))
            else:
                results.append((True, "duplicate gateway", "only one running"))
            proc_home = _gateway_proc_home(pid)
            if proc_home is None:
                results.append((False, "gateway home",
                                f"could not read /proc/{pid}/environ"))
            elif Path(proc_home).resolve() == mercury_home.resolve():
                results.append((True, "gateway home",
                                f"gateway runs with this home ({proc_home})"))
            else:
                results.append((False, "gateway home",
                                f"gateway runs with {proc_home} but doctor "
                                f"checked {mercury_home} — wrong instance: "
                                "restart the gateway from this home"))
        else:
            results.append((False, "gateway process",
                            "no live gateway PID — start it: "
                            f"{os.environ.get('MERCURY_CMD', '').strip() or 'mercury'} gateway start"))
    except Exception:
        results.append((False, "gateway process", "could not check (status module unavailable)"))
    try:
        from gateway.config import load_gateway_config

        gcfg = load_gateway_config()
        plats = {getattr(p, "value", str(p)): bool(c.enabled)
                 for p, c in (gcfg.platforms or {}).items()}
        if plats.get("irc"):
            results.append((True, "gateway platforms", "irc adapter enabled"))
        elif plats:
            results.append((False, "gateway platforms",
                            f"irc adapter NOT enabled (enabled: "
                            f"{', '.join(sorted(k for k, v in plats.items() if v)) or 'none'}) — "
                            "run setup gateway to enable IRC"))
        else:
            results.append((False, "gateway platforms",
                            "no platforms configured at all — "
                            "run setup gateway to enable IRC"))
    except Exception as exc:
        results.append((False, "gateway platforms",
                        f"could not load gateway config ({exc})"))
    if agent_up:
        conns = _established_to(agent_port)
        if conns:
            results.append((True, "bot connection",
                            f"{conns} established TCP connection(s) to the agent port"))
        else:
            snippets = _gateway_log_snippets(mercury_home)
            if snippets:
                last = snippets[-1]
                tail = last[-160:] if len(last) > 160 else last
                results.append((False, "bot connection",
                                f"nothing connected; gateway log says: {tail}"))
            else:
                results.append((False, "bot connection",
                                "nothing is connected to the agent port and the "
                                "gateway log shows no IRC adapter lines at all — "
                                "the adapter never started"))

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
