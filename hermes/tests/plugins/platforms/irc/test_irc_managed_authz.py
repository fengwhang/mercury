"""The observatory-managed IRC adapter declares trusted-upstream authz."""

from __future__ import annotations

from types import SimpleNamespace


def test_adapter_declares_upstream_only_for_observatory(tmp_path, monkeypatch) -> None:
    from plugins.platforms.irc.adapter import IRCAdapter

    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setenv("IRC_SERVER", "127.0.0.1")
    monkeypatch.setenv("IRC_CHANNEL", "#x")
    monkeypatch.delenv("IRC_MANAGED_BY", raising=False)

    adapter = IRCAdapter(SimpleNamespace(extra={}))
    assert adapter.authorization_is_upstream is False  # public IRC: gated

    monkeypatch.setenv("IRC_MANAGED_BY", "observatory")
    adapter = IRCAdapter(SimpleNamespace(extra={}))
    assert adapter.authorization_is_upstream is True  # perimeter is auth

    monkeypatch.delenv("IRC_MANAGED_BY")
    adapter = IRCAdapter(SimpleNamespace(extra={"managed_by": "observatory"}))
    assert adapter.authorization_is_upstream is True
