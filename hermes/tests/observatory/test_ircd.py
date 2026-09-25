"""IRC daemon tests: join/msg fanout, server replay, destroy, auth."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from observatory.ircd import DaemonConfig, IrcDaemon, clean_channel, clean_nick


def test_clean_channel() -> None:
    assert clean_channel("parent-child") == "#parent-child"
    assert clean_channel("My Agent!") == "#my-agent"
    assert clean_channel("") == "#unnamed"


def test_clean_nick() -> None:
    assert clean_nick("my gateway") == "my_gateway"
    assert clean_nick("") == "agent"


class RawClient:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        buf = b""
        try:
            while not self.reader.at_eof():  # type: ignore[union-attr]
                data = await self.reader.read(4096)  # type: ignore[union-attr]
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    await self.lines.put(
                        raw.decode("utf-8", errors="replace").rstrip("\r")
                    )
        except asyncio.CancelledError:
            pass

    async def send(self, line: str) -> None:
        assert self.writer is not None
        self.writer.write((line + "\r\n").encode())
        await self.writer.drain()

    async def register(self, nick: str, password: str = "") -> None:
        if password:
            await self.send(f"PASS {password}")
        await self.send(f"NICK {nick}")
        await self.send(f"USER {nick} 0 * :test")
        for _ in range(100):
            line = await asyncio.wait_for(self.lines.get(), timeout=5)
            if " 001 " in line:
                return
        raise AssertionError("no welcome")

    async def next_match(self, fragment: str, timeout: float = 5.0) -> str:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = end - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for {fragment!r}"
            line = await asyncio.wait_for(self.lines.get(), timeout=remaining)
            if fragment in line:
                return line

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass


@asynccontextmanager
async def running_daemon(tmp_path, **kwargs):
    """Start an IrcDaemon on ephemeral ports (fixtures stay sync per convention)."""
    config = DaemonConfig(
        agent_port=0, server_port=0, state_dir=str(tmp_path), **kwargs
    )
    d = IrcDaemon(config)
    await d.start()
    agent_port = d._servers[0].sockets[0].getsockname()[1]
    server_port = d._servers[1].sockets[0].getsockname()[1]
    try:
        yield d, agent_port, server_port
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_join_and_privmsg_fanout(tmp_path) -> None:
    async with running_daemon(tmp_path) as (d, agent_port, _):
        a, b = RawClient(), RawClient()
        await a.connect(agent_port)
        await b.connect(agent_port)
        try:
            await a.register("alice")
            await b.register("bob")
            await a.send("JOIN #parent-child")
            await a.next_match("JOIN #parent-child")
            await b.send("JOIN #parent-child")
            await b.next_match("JOIN #parent-child")
            await a.send("PRIVMSG #parent-child :hello bob")
            got = await b.next_match("PRIVMSG #parent-child :hello bob")
            assert "alice" in got
            assert [m.text for m in d.channel_history("#parent-child")] == ["hello bob"]
        finally:
            await a.close()
            await b.close()


@pytest.mark.asyncio
async def test_server_replay_on_join(tmp_path) -> None:
    async with running_daemon(tmp_path) as (_, agent_port, server_port):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("bot")
            await a.send("JOIN #replay")
            await a.next_match("JOIN #replay")
            await a.send("PRIVMSG #replay :first")
            await a.send("PRIVMSG #replay :second")
            await asyncio.sleep(0.2)
        finally:
            await a.close()
        u = RawClient()
        await u.connect(server_port)
        try:
            await u.register("user")
            await u.send("JOIN #replay")
            await u.next_match("JOIN #replay")
            got1 = await u.next_match(":first")
            got2 = await u.next_match(":second")
            assert "PRIVMSG #replay" in got1 and "PRIVMSG #replay" in got2
        finally:
            await u.close()


@pytest.mark.asyncio
async def test_destroy_channel_parts_members(tmp_path) -> None:
    async with running_daemon(tmp_path) as (d, agent_port, _):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("JOIN #doomed")
            await a.next_match("JOIN #doomed")
            removed = await d.destroy_channel("#doomed")
            assert removed == 1
            assert await a.next_match("PART #doomed")
            assert d.channel_names() == []
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_server_password_enforced(tmp_path) -> None:
    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        u = RawClient()
        await u.connect(server_port)
        try:
            await u.send("NICK user")
            await u.send("USER user 0 * :test")
            # No 464 before an auth attempt: clients latch an early 464
            # as fatal (Goguma). A NOTICE (automaton-safe) instead.
            assert "needs PASS" in await u.next_match("NOTICE", timeout=2.0)
            with pytest.raises(TimeoutError):
                await u.next_match("464", timeout=0.5)
            await u.send("PASS wrong")
            assert await u.next_match("464")
        finally:
            await u.close()
        v = RawClient()
        await v.connect(server_port)
        try:
            await v.register("user2", password="s3cret")
        finally:
            await v.close()


@pytest.mark.asyncio
async def test_history_survives_restart(tmp_path) -> None:
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("JOIN #persist")
            await a.next_match("JOIN #persist")
            await a.send("PRIVMSG #persist :remember me")
            await asyncio.sleep(0.3)
        finally:
            await a.close()
    async with running_daemon(tmp_path) as (d2, _, __):
        assert [m.text for m in d2.channel_history("#persist")] == ["remember me"]


@pytest.mark.asyncio
async def test_oper_destroy_kills_room(tmp_path) -> None:
    async with running_daemon(tmp_path, agent_password="op-secret") as (
        d,
        agent_port,
        _,
    ):
        bot = RawClient()
        await bot.connect(agent_port)
        mem = RawClient()
        await mem.connect(agent_port)
        try:
            await bot.register("bot", password="op-secret")
            await mem.register("mem", password="op-secret")
            await bot.send("JOIN #doomed")
            await bot.next_match("JOIN #doomed")
            await mem.send("JOIN #doomed")
            await mem.next_match("JOIN #doomed")
            # unprivileged destroy is refused
            await mem.send("DESTROY #doomed")
            await mem.next_match("481")
            assert "#doomed" in d.channel_names()
            # oper destroy PARTs members and drops history
            await bot.send("OPER op-secret")
            await bot.next_match("381")
            await bot.send("DESTROY #doomed")
            await mem.next_match("PART #doomed")
            assert d.channel_names() == []
            assert d.channel_history("#doomed") == []
        finally:
            await bot.close()
            await mem.close()


def _args(**kw):
    import types

    base = dict(
        host=None,
        agent_port=None,
        server_host=None,
        server_port=None,
        server_name=None,
        password=None,
        agent_password=None,
        history_limit=None,
        state_dir="",
        config="",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_resolve_layers_argv_over_file_over_defaults(tmp_path) -> None:
    import json

    from observatory.ircd import _resolve_daemon_config

    cfg_file = tmp_path / "ircd.json"
    cfg_file.write_text(
        json.dumps({"server_host": "100.64.0.1", "server_port": 6670}),
        encoding="utf-8",
    )
    cfg = _resolve_daemon_config(_args(config=str(cfg_file)))
    assert cfg.server_host == "100.64.0.1"
    assert cfg.agent_port == 6669  # compiled default fills gaps
    assert cfg.tls_port == 6697  # absent file key falls back to default


def test_resolve_honors_legacy_keys(tmp_path) -> None:
    import json

    from observatory.ircd import _resolve_daemon_config

    cfg_file = tmp_path / "ircd.json"
    cfg_file.write_text(
        json.dumps({"bouncer_host": "100.64.0.1", "bouncer_port": 6670}),
        encoding="utf-8",
    )
    cfg = _resolve_daemon_config(_args(config=str(cfg_file)))
    assert cfg.server_host == "100.64.0.1"
    assert cfg.server_port == 6670


@pytest.mark.asyncio
async def test_legacy_keys_bind_server_listener(tmp_path) -> None:
    """Pre-rename ircd.json (old client-listener keys) still binds."""
    import json

    from observatory.ircd import _resolve_daemon_config

    cfg_file = tmp_path / "ircd.json"
    cfg_file.write_text(
        json.dumps({"bouncer_host": "127.0.0.1", "bouncer_port": 0}),
        encoding="utf-8",
    )
    cfg = _resolve_daemon_config(_args(config=str(cfg_file)))
    assert cfg.server_host == "127.0.0.1"
    cfg.agent_port = 0
    cfg.state_dir = str(tmp_path)
    d = IrcDaemon(cfg)
    await d.start()
    try:
        assert len(d._servers) >= 2
        port = d._servers[1].sockets[0].getsockname()[1]
        c = RawClient()
        await c.connect(port)
        try:
            await c.register("legacy")
            await c.send("JOIN #legacy")
            await c.next_match("JOIN #legacy")
        finally:
            await c.close()
    finally:
        await d.stop()


def test_resolve_agent_host_pin(tmp_path) -> None:
    """set_ircd_bind's `agent_host` moves the agent listener (#1)."""
    import json

    from observatory.ircd import _resolve_daemon_config

    cfg_file = tmp_path / "ircd.json"
    cfg_file.write_text(
        json.dumps({"agent_host": "100.64.0.1", "server_name": "vm"}),
        encoding="utf-8",
    )
    cfg = _resolve_daemon_config(_args(config=str(cfg_file)))
    assert cfg.host == "100.64.0.1"
    # explicit --host still wins; legacy `host` key still works
    cfg = _resolve_daemon_config(_args(config=str(cfg_file), host="10.0.0.9"))
    assert cfg.host == "10.0.0.9"
    cfg_file.write_text(json.dumps({"host": "10.0.0.8"}), encoding="utf-8")
    assert _resolve_daemon_config(_args(config=str(cfg_file))).host == "10.0.0.8"


def test_resolve_state_dir_config(tmp_path) -> None:
    import json

    from observatory.ircd import _resolve_daemon_config

    state_dir = tmp_path / "observatory"
    state_dir.mkdir()
    (state_dir / "ircd.json").write_text(
        json.dumps({"server_name": "vm"}), encoding="utf-8"
    )
    cfg = _resolve_daemon_config(_args(state_dir=str(state_dir)))
    assert cfg.server_name == "vm"
    assert cfg.state_dir == str(state_dir)


def test_unit_passes_only_state_dir(tmp_path) -> None:
    from observatory.config_gen import render_observatory_unit

    unit = render_observatory_unit(
        python_bin="/x/bin/python",
        hermes_root="/x/hermes",
        mercury_home="/h",
        log_dir="/h/observatory/logs",
    )
    assert "--state-dir /h/observatory" in unit
    assert "--server-host" not in unit
    assert "--agent-port" not in unit


@pytest.mark.asyncio
async def test_server_bind_failure_degrades_not_dies(tmp_path) -> None:
    """Unbindable server (Tailscale down) must not take the agent down."""
    from observatory.ircd import DaemonConfig, IrcDaemon

    d = IrcDaemon(
        DaemonConfig(
            agent_port=0,
            server_host="203.0.113.1",
            server_port=0,
            state_dir=str(tmp_path),
        )
    )
    await d.start()
    try:
        assert len(d._servers) == 1
        port = d._servers[0].sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"NICK a\r\nUSER a 0 * :t\r\n")
        await writer.drain()
        await asyncio.sleep(0.3)
        assert d._clients["a"].registered
        writer.close()
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_both_listeners_down_raises(tmp_path) -> None:
    """Nothing bindable at all is still a hard error (no silent no-op)."""
    import socket

    from observatory.ircd import DaemonConfig, IrcDaemon

    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    d = IrcDaemon(
        DaemonConfig(
            agent_port=port, server_host="127.0.0.1", server_port=port,
            state_dir=str(tmp_path),
        )
    )
    try:
        with pytest.raises(OSError):
            await d.start()
    finally:
        held.close()
        await d.stop()


@pytest.mark.asyncio
async def test_sasl_plain_login(tmp_path) -> None:
    import base64

    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            await c.send("CAP LS 302")
            got = await c.next_match("CAP ")
            assert "sasl" in got
            await c.send("CAP REQ :sasl")
            await c.next_match("ACK :sasl")
            await c.send("AUTHENTICATE PLAIN")
            await c.next_match("AUTHENTICATE +")
            blob = base64.b64encode(b"user\x00user\x00s3cret").decode()
            await c.send(f"AUTHENTICATE {blob}")
            await c.next_match("903")
            await c.send("NICK sasluser")
            await c.send("USER sasluser 0 * :t")
            await c.next_match(" 001 ")
            await c.send("JOIN #saslroom")
            await c.next_match("JOIN #saslroom")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_sasl_wrong_password_stays_out(tmp_path) -> None:
    import base64

    async with running_daemon(tmp_path, password="s3cret") as (d, _, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            await c.send("AUTHENTICATE PLAIN")
            await c.next_match("AUTHENTICATE +")
            blob = base64.b64encode(b"user\x00user\x00wrong").decode()
            await c.send(f"AUTHENTICATE {blob}")
            await c.next_match("904")
            await c.send("NICK nosuch")
            await c.send("USER nosuch 0 * :t")
            await asyncio.sleep(0.3)
            assert "nosuch" in d._clients  # nick reserved…
            assert d._clients["nosuch"].registered is False  # …but never registered
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_sasl_abort(tmp_path) -> None:
    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            await c.send("AUTHENTICATE PLAIN")
            await c.next_match("AUTHENTICATE +")
            await c.send("AUTHENTICATE *")
            await c.next_match("906")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_tls_listener_serves_strict_clients(tmp_path) -> None:
    """A TLS-only client (Goguma-style) registers and joins over TLS."""
    import ssl

    from observatory import provision as _prov
    from observatory.ircd import DaemonConfig, IrcDaemon

    home = tmp_path / "mercury"
    (home / "observatory").mkdir(parents=True)
    import os as _os

    old_home = _os.environ.get("MERCURY_HOME")
    _os.environ["MERCURY_HOME"] = str(home)
    try:
        _prov.ensure_tls_cert(home)
    finally:
        if old_home is None:
            _os.environ.pop("MERCURY_HOME", None)
        else:
            _os.environ["MERCURY_HOME"] = old_home
    from observatory.config_gen import ObservatoryPaths

    paths = ObservatoryPaths(home)
    ctx = ssl.create_default_context(cafile=str(paths.tls_ca))
    d = None
    import socket as _socket_mod

    probe = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_tls_port = probe.getsockname()[1]
    probe.close()
    d = IrcDaemon(
        DaemonConfig(
            agent_port=0,
            server_port=0,
            tls_port=free_tls_port,
            tls_cert=str(paths.tls_cert),
            tls_key=str(paths.tls_key),
            state_dir=str(home / "observatory"),
        )
    )
    await d.start()
    try:
        # find the TLS listener by its bound port
        tls_port = None
        for server in d._servers:
            for sock in server.sockets or []:
                if sock.getsockname()[1] == free_tls_port:
                    tls_port = free_tls_port
        assert tls_port is not None, [s.sockets for s in d._servers]
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", tls_port, ssl=ctx, server_hostname="localhost"
        )
        got: list[str] = []

        async def _pump() -> None:
            buf = b""
            while not reader.at_eof():
                data = await reader.read(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    got.append(raw.decode("utf-8", errors="replace").rstrip("\r"))

        pump = asyncio.create_task(_pump())

        async def _send(line: str) -> None:
            writer.write((line + "\r\n").encode())
            await writer.drain()

        async def _wait(fragment: str) -> str:
            for _ in range(100):
                for line in list(got):
                    if fragment in line:
                        return line
                await asyncio.sleep(0.05)
            raise AssertionError(f"never saw {fragment!r}: {got[-3:]}")

        await _send("NICK tlsuser")
        await _send("USER tlsuser 0 * :t")
        await _wait(" 001 ")
        await _send("JOIN #tlsroom")
        await _wait("JOIN #tlsroom")
        pump.cancel()
        writer.close()
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_pass_last_order_registers(tmp_path) -> None:
    """NICK/USER before PASS (the Goguma order) must still register."""
    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            await c.send("NICK late")
            await c.send("USER late 0 * :test")
            # Silence — no 464 before the password arrives.
            with pytest.raises(TimeoutError):
                await c.next_match("464", timeout=0.5)
            await c.send("PASS s3cret")
            assert await c.next_match(" 001 ", timeout=5.0)
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_sasl_after_nick_user_registers(tmp_path) -> None:
    """SASL PLAIN after NICK/USER must complete registration (903 → 001)."""
    import base64

    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            await c.send("NICK sasluser")
            await c.send("USER sasluser 0 * :test")
            # Silence — no 464 before SASL completes.
            with pytest.raises(TimeoutError):
                await c.next_match("464", timeout=0.5)
            await c.send("CAP REQ :sasl")
            assert await c.next_match("ACK :sasl")
            await c.send("AUTHENTICATE PLAIN")
            assert await c.next_match("AUTHENTICATE +")
            blob = base64.b64encode(b"sasluser\x00sasluser\x00s3cret").decode()
            await c.send(f"AUTHENTICATE {blob}")
            assert await c.next_match(" 903 ")
            assert await c.next_match(" 001 ", timeout=5.0)
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_failed_pass_logs_shape_not_secret(tmp_path, caplog) -> None:
    """A wrong PASS logs attempt shape (lengths) but never the secret."""
    import logging

    async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
        c = RawClient()
        await c.connect(server_port)
        try:
            with caplog.at_level(logging.INFO, logger="observatory.ircd"):
                await c.send("NICK nosy")
                await c.send("USER nosy 0 * :test")
                await c.send("PASS wrong")
                assert await c.next_match("464")
        finally:
            await c.close()
    assert "PASS attempt len=5 expected=6 -> 464" in caplog.text
    assert "wrong" not in caplog.text
    assert "s3cret" not in caplog.text


@pytest.mark.asyncio
async def test_list_discovers_rooms(tmp_path) -> None:
    """LIST returns 321/322/323 with member counts; empty server lists none."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("LIST")
            assert await a.next_match(" 321 ")
            assert await a.next_match(" 323 ")
            b = RawClient()
            await b.connect(agent_port)
            try:
                await b.register("bob")
                await b.send("JOIN #room")
                assert await b.next_match("JOIN #room")
                await asyncio.sleep(0.3)
                await a.send("LIST")
                assert await a.next_match(" 321 ")
                listed = await a.next_match(" 322 ")
                assert "#room" in listed and " 1 " in listed
                assert await a.next_match(" 323 ")
            finally:
                await b.close()
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_welcome_ends_with_no_motd(tmp_path) -> None:
    """Registration terminates with 422 so clients stop waiting for MOTD."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        c = RawClient()
        await c.connect(agent_port)
        try:
            await c.send("NICK mott")
            await c.send("USER mott 0 * :test")
            assert await c.next_match(" 001 ")
            assert await c.next_match(" 422 ")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_cap_negotiates_subset(tmp_path) -> None:
    """CAP LS advertises v3 caps; REQ acks the known, naks the rest."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        c = RawClient()
        await c.connect(agent_port)
        try:
            await c.send("CAP LS 302")
            ls = await c.next_match("CAP ")
            for cap in ("sasl", "message-tags", "server-time", "batch",
                        "echo-message", "labeled-response", "draft/chathistory"):
                assert cap in ls
            await c.send("CAP REQ :sasl draft/chathistory bogus-cap")
            ack = await c.next_match("ACK ")
            assert "sasl" in ack and "draft/chathistory" in ack
            nak = await c.next_match("NAK ")
            assert "bogus-cap" in nak
            await c.send("CAP END")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_labeled_privmsg_routes_and_echoes(tmp_path) -> None:
    """@label PRIVMSG still routes; echo-message returns it with the label."""
    async with running_daemon(tmp_path) as (d, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("alice")
            await a.send("CAP REQ :echo-message labeled-response message-tags")
            assert await a.next_match("ACK ")
            await a.send("JOIN #echo")
            assert await a.next_match("JOIN #echo")
            await a.send("@label=xyz PRIVMSG #echo :hello")
            echo = await a.next_match("PRIVMSG #echo :hello")
            assert "@label=xyz" in echo or "label=xyz" in echo
            assert "alice" in d.channel_history("#echo")[-1].sender
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_server_time_tagged_only_when_negotiated(tmp_path) -> None:
    """Relayed lines carry @time only for server-time clients."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("anna")
            await a.send("JOIN #t")
            assert await a.next_match("JOIN #t")
            b = RawClient()
            await b.connect(agent_port)
            try:
                await b.register("bob")
                await b.send("CAP REQ :server-time message-tags")
                assert await b.next_match("ACK ")
                await b.send("JOIN #t")
                assert await b.next_match("JOIN #t")
                await a.send("PRIVMSG #t :hi bob")
                got = await b.next_match("PRIVMSG #t :hi bob")
                assert "@time=" in got
            finally:
                await b.close()
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_chathistory_latest_batch(tmp_path) -> None:
    """CHATHISTORY LATEST returns a framed batch of backlog."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("hist")
            await a.send("CAP REQ :batch message-tags server-time")
            assert await a.next_match("ACK ")
            await a.send("JOIN #h")
            assert await a.next_match("JOIN #h")
            await a.send("PRIVMSG #h :one")
            await a.send("PRIVMSG #h :two")
            await asyncio.sleep(0.3)
            await a.send("CHATHISTORY LATEST #h * 10")
            start = await a.next_match("BATCH +")
            assert "draft/chathistory #h" in start
            first = await a.next_match("PRIVMSG #h :one")
            assert "msgid=" in first
            assert await a.next_match("PRIVMSG #h :two")
            assert await a.next_match("BATCH -")
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_away_set_unset_and_who_flag(tmp_path) -> None:
    """AWAY toggles 306/305 (soju sends it on upstream connect)."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        c = RawClient()
        await c.connect(agent_port)
        try:
            await c.register("awaynick")
            await c.send("AWAY :gone fishing")
            assert await c.next_match(" 306 ")
            await c.send("JOIN #aw")
            assert await c.next_match("JOIN #aw")
            await c.send("WHO #aw")
            who = await c.next_match(" 352 ")
            assert " G" in who or " G " in who or who.rstrip().endswith(" G")
            await c.send("AWAY")
            assert await c.next_match(" 305 ")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_invite_relays_and_acks(tmp_path) -> None:
    """INVITE delivers to a local nick with 341 to the sender."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("inviter")
            await a.send("JOIN #inv")
            assert await a.next_match("JOIN #inv")
            b = RawClient()
            await b.connect(agent_port)
            try:
                await b.register("invitee")
                await a.send("INVITE invitee :#inv")
                assert await a.next_match(" 341 ")
                got = await b.next_match("INVITE")
                assert "#inv" in got
            finally:
                await b.close()
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_invite_rejects_unknown(tmp_path) -> None:
    async with running_daemon(tmp_path) as (_, agent_port, __):
        c = RawClient()
        await c.connect(agent_port)
        try:
            await c.register("lonely")
            await c.send("JOIN #inv")
            assert await c.next_match("JOIN #inv")
            await c.send("INVITE ghost :#inv")
            assert await c.next_match(" 401 ")
            await c.send("INVITE lonely :#nope")
            assert await c.next_match(" 403 ")
        finally:
            await c.close()


@pytest.mark.asyncio
async def test_invite_auto_joins_target(tmp_path) -> None:
    """INVITE server-joins the target: self-JOIN + broadcast + names."""
    async with running_daemon(tmp_path) as (_, agent_port, __):
        a = RawClient()
        await a.connect(agent_port)
        try:
            await a.register("inviter")
            await a.send("JOIN #auto")
            assert await a.next_match("JOIN #auto")
            b = RawClient()
            await b.connect(agent_port)
            try:
                await b.register("invitee")
                await a.send("INVITE invitee :#auto")
                assert await a.next_match(" 341 ")
                assert "#auto" in await b.next_match("INVITE")
                # target sees its own JOIN + names ...
                joined = await b.next_match("JOIN #auto")
                assert "invitee!" in joined
                assert await b.next_match(" 353 ")
                # ... and the existing member sees the broadcast
                seen = await a.next_match("JOIN #auto")
                assert "invitee!" in seen
            finally:
                await b.close()
        finally:
            await a.close()


@pytest.mark.asyncio
async def test_oper_accepts_either_listener_secret(tmp_path) -> None:
    """OPER with the server password works when both set."""
    async with running_daemon(
        tmp_path, password="s3cret", agent_password="op-secret"
    ) as (_, agent_port, __):
        bot = RawClient()
        await bot.connect(agent_port)
        try:
            await bot.register("bot", password="op-secret")
            await bot.send("OPER s3cret")
            assert await bot.next_match("381")
            await bot.send("OPER wrong")
            assert await bot.next_match("464")
        finally:
            await bot.close()


@pytest.mark.asyncio
async def test_register_auto_joins_gateway_room(tmp_path) -> None:
    """Every authenticated user-listener client lands in #*_gateway —
    local or remote. Agent-listener bots do not."""
    async with running_daemon(tmp_path, password="s3cret") as (_, agent_port, server_port):
        u = RawClient()
        await u.connect(server_port)
        try:
            await u.register("remote", password="s3cret")
            join = await u.next_match("JOIN #", timeout=5.0)
            assert "_gateway" in join
        finally:
            await u.close()
        bot = RawClient()
        await bot.connect(agent_port)
        try:
            await bot.register("bot")
            with pytest.raises(TimeoutError):
                await bot.next_match("JOIN #", timeout=0.5)
        finally:
            await bot.close()


@pytest.mark.asyncio
async def test_register_auto_joins_all_live_rooms(tmp_path) -> None:
    """Zero manual joins: registration pulls every live agent room."""
    from observatory import rooms as rooms_mod
    from observatory.state import ObservatoryState

    state = ObservatoryState(tmp_path / "state.db")
    row = state.add_node(
        "orch-1", engine="hermes", name="bravo", slug="bravo",
        mxid="bravo", session_ref="#bravo",
    )
    state.set_room_id(row["node_id"], "#bravo")
    manager = rooms_mod.RoomManager(state, None)
    rooms_mod.set_room_manager(manager)
    try:
        async with running_daemon(tmp_path, password="s3cret") as (_, __, server_port):
            u = RawClient()
            await u.connect(server_port)
            try:
                await u.register("remote", password="s3cret")
                joins = []
                for _ in range(2):
                    joins.append(await u.next_match("JOIN #", timeout=5.0))
                blob = "\n".join(joins)
                assert "_gateway" in blob
                assert "#bravo" in blob
            finally:
                await u.close()
    finally:
        rooms_mod.set_room_manager(None)


@pytest.mark.asyncio
async def test_failed_listener_rebinds_when_port_frees(tmp_path) -> None:
    """Boot race cover: a bind that fails at start (tailscaled not up)
    recovers in the background once the port is free."""
    import socket

    from observatory.ircd import DaemonConfig, IrcDaemon

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    blocked_port = blocker.getsockname()[1]
    config = DaemonConfig(
        agent_port=0, server_host="127.0.0.1", server_port=blocked_port,
        state_dir=str(tmp_path),
    )
    d = IrcDaemon(config, rebind_interval=0.1)
    await d.start()
    try:
        assert len(d._servers) == 1  # agent bound, server deferred
        assert d._pending_binds, "server bind must be pending retry"
        blocker.close()
        for _ in range(100):
            if not d._pending_binds:
                break
            await asyncio.sleep(0.1)
        assert not d._pending_binds
        assert len(d._servers) == 2
        c = RawClient()
        await c.connect(blocked_port)
        try:
            await c.register("late")  # 001 consumed here — registration done
        finally:
            await c.close()
    finally:
        await d.stop()
