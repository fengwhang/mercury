"""IRC must configure from the .env file when the process env is empty.

Regression: service gateways whose os.environ lacks .env-derived keys
(the VM's gateway process carries zero IRC_* despite a wired .env)
silently skipped IRC because every probe read raw os.getenv.
"""

from __future__ import annotations


def test_is_connected_without_process_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    (tmp_path / ".env").write_text(
        "IRC_SERVER=127.0.0.1\n"
        "IRC_CHANNEL=#vm_gateway\n"
        "IRC_NICKNAME=vm_gateway\n"
        "IRC_PORT=6669\n",
        encoding="utf-8",
    )
    for key in ("IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME", "IRC_PORT",
                "IRC_USE_TLS", "IRC_SERVER_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    from gateway.config import PlatformConfig
    from plugins.platforms.irc import adapter as irc_mod

    assert irc_mod.is_connected(PlatformConfig(enabled=True)) is True
    assert irc_mod.check_requirements() is True


def test_bang_to_slash_known_verbs_only() -> None:
    from plugins.platforms.irc.adapter import bang_to_slash

    assert bang_to_slash("!spawn agent") == "/spawn agent"
    assert bang_to_slash("!SPAWNOMP x") == "/spawnomp x"
    assert bang_to_slash("!exit") == "/exit"
    assert bang_to_slash("!stop now") == "/stop now"
    assert bang_to_slash("!approve") == "/approve"
    assert bang_to_slash("!deny no") == "/deny no"
    # Not verbs: untouched chat.
    assert bang_to_slash("!wow amazing") == "!wow amazing"
    assert bang_to_slash("hello!") == "hello!"
    assert bang_to_slash("!!spawn x") == "!!spawn x"
    assert bang_to_slash("/spawn x") == "/spawn x"
    assert bang_to_slash("!") == "!"
