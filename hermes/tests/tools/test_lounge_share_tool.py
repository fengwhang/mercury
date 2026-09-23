"""Tests for the Lounge paperclip tool (agent file sharing)."""

import asyncio
import json


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_registered_under_irc_toolset():
    """Plugin-platform agents only see tools tagged with the bare platform."""
    from tools import lounge_share_tool as _  # noqa: F401 (registration)
    from tools.registry import registry

    entry = registry.get_entry("lounge_share")
    assert entry is not None
    assert entry.toolset == "irc"


def test_resolves_through_platform_toolsets():
    from gateway.platform_registry import platform_registry
    from toolsets import resolve_toolset

    platform_registry.register_deferred("irc", lambda: None)
    try:
        names = resolve_toolset("mercury-irc")
    finally:
        platform_registry.unregister("irc")
    assert "lounge_share" in names


def test_posts_link_in_current_channel_only(tmp_path, monkeypatch):
    import observatory.lounge as lounge_mod
    import observatory.rooms as rooms_mod
    from gateway import session_context as session_ctx
    from tools import lounge_share_tool as share_mod

    home = tmp_path / "mercury"
    monkeypatch.setenv("MERCURY_HOME", str(home))
    src = tmp_path / "notes.txt"
    src.write_text("hello")
    monkeypatch.setattr(
        session_ctx, "get_session_env",
        lambda name, default="": "#vm_ace" if "CHAT_ID" in name else default)
    monkeypatch.setattr(
        lounge_mod, "stage_lounge_upload",
        lambda home, path: {"url_path": "uploads/ab/cdef/notes.txt",
                            "filename": "notes.txt"})
    monkeypatch.setattr(
        lounge_mod, "lounge_base_url", lambda home=None: "http://h:9000")
    monkeypatch.setattr(lounge_mod, "check_upload_serves", lambda *a: True)
    posted = []
    monkeypatch.setattr(
        rooms_mod, "say_nowait",
        lambda channel, text: posted.append((channel, text)) or True)
    out = json.loads(_run(share_mod._handle_lounge_share(
        {"path": str(src), "caption": "read this"})))
    assert out["success"] is True
    assert out["url"] == "http://h:9000/uploads/ab/cdef/notes.txt"
    assert out["channel"] == "#vm_ace"
    assert posted == [("#vm_ace",
                       "read this\nhttp://h:9000/uploads/ab/cdef/notes.txt")]


def test_refuses_outside_irc(monkeypatch):
    from gateway import session_context as session_ctx
    from tools import lounge_share_tool as share_mod

    monkeypatch.setattr(
        session_ctx, "get_session_env", lambda name, default="": "")
    out = json.loads(_run(share_mod._handle_lounge_share({"path": "/x"})))
    assert out["success"] is False
    assert "IRC channels only" in out["error"]


def test_dead_link_not_posted(tmp_path, monkeypatch):
    import observatory.lounge as lounge_mod
    import observatory.rooms as rooms_mod
    from gateway import session_context as session_ctx
    from tools import lounge_share_tool as share_mod

    monkeypatch.setattr(
        session_ctx, "get_session_env",
        lambda name, default="": "#vm_ace" if "CHAT_ID" in name else default)
    monkeypatch.setattr(
        lounge_mod, "stage_lounge_upload",
        lambda home, path: {"url_path": "uploads/ab/cdef/x.txt",
                            "filename": "x.txt"})
    monkeypatch.setattr(
        lounge_mod, "lounge_base_url", lambda home=None: "http://h:9000")
    monkeypatch.setattr(lounge_mod, "check_upload_serves", lambda *a: False)
    posted = []
    monkeypatch.setattr(
        rooms_mod, "say_nowait",
        lambda channel, text: posted.append((channel, text)) or True)
    out = json.loads(_run(share_mod._handle_lounge_share({"path": "/x"})))
    assert out["success"] is False
    assert "does not serve" in out["error"]
    assert posted == []


def test_check_upload_serves_probes() -> None:
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from observatory import lounge as lounge_mod

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"x")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert lounge_mod.check_upload_serves(
            f"http://127.0.0.1:{srv.server_port}/uploads/ab/cd") is True
    finally:
        srv.shutdown()
    assert lounge_mod.check_upload_serves("http://127.0.0.1:1/") is False
