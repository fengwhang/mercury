"""The Lounge provisioner: config render, paths, user flow (mocked)."""

from __future__ import annotations

from observatory import lounge as lounge_mod


def test_render_lounge_config() -> None:
    conf = lounge_mod.render_lounge_config(host="100.9.9.9", port=9000)
    assert 'host: "100.9.9.9"' in conf
    assert "port: 9000" in conf
    assert "public: false" in conf


def test_ensure_lounge_config_idempotent(tmp_path) -> None:
    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    assert lounge_mod.ensure_lounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "wrote"
    assert lounge_mod.ensure_lounge_config(
        paths, host="127.0.0.1", port=9000)["action"] == "current"
    assert lounge_mod.ensure_lounge_config(
        paths, host="100.9.9.9", port=9000)["action"] == "updated"


def test_ensure_lounge_user_current_when_present(tmp_path) -> None:
    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    users = paths.home / "users"
    users.mkdir(parents=True)
    (users / "owner.json").write_text("{}")
    assert lounge_mod.ensure_lounge_user(paths, "owner", None) == {
        "action": "current"}


def test_ensure_lounge_user_needs_password(tmp_path) -> None:
    import pytest

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    with pytest.raises(lounge_mod.LoungeError):
        lounge_mod.ensure_lounge_user(paths, "owner", None)


def test_status_reports_bind(tmp_path) -> None:
    from observatory import lounge as lounge_mod

    paths = lounge_mod.LoungePaths(tmp_path / "mercury")
    lounge_mod.ensure_lounge_config(paths, host="100.9.9.9", port=9000)
    st = lounge_mod.status_lounge(tmp_path / "mercury")
    assert st["configured"] is True
    assert st["host"] == "100.9.9.9"
    assert st["port"] == 9000


def test_status_external_when_port_answers(tmp_path, monkeypatch) -> None:
    from observatory import lounge as lounge_mod

    monkeypatch.setattr(lounge_mod, "lounge_port_open",
                        lambda *a, **k: True)
    st = lounge_mod.status_lounge(tmp_path / "mercury")
    assert st["configured"] is False
    assert st["external"] is True


def test_lounge_port_open_closed() -> None:
    from observatory import lounge as lounge_mod

    assert lounge_mod.lounge_port_open("127.0.0.1", 1) is False


def test_local_port_answers_loopback() -> None:
    import asyncio as _asyncio
    from observatory import lounge as lounge_mod

    async def _probe() -> None:
        srv = await _asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            assert lounge_mod._local_port_answers(port) is True
        finally:
            srv.close()
        assert lounge_mod._local_port_answers(port) is False

    _asyncio.run(_probe())
