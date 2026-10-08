"""Pending transport cancellation must not outlive status/start work."""

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from websockets.sync.client import connect

from observatory import voice_call_stt as stt


@pytest.mark.parametrize("phase", ["status", "start"])
def test_pending_transport_close_reconciles_only_inflight_owner(monkeypatch, phase):
    state = stt.SidecarState("http://offline-authority", {}, token="private-fixture")
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    finished = threading.Event()
    actions = []

    def request(path, payload=None, **_):
        gated = (phase == "status" and payload is None) or (
            phase == "start" and payload and payload.get("action") == "start"
        )
        if gated:
            entered.set()
            assert release.wait(2)
        if payload:
            actions.append(payload)
        return {"ok": True, "allowed": True, "engine": "hermes"}

    monkeypatch.setattr(state, "request", request)
    original_close = stt.WsConnection.close

    def observe_close(ws, *args, **kwargs):
        original_close(ws, *args, **kwargs)
        closed.set()

    monkeypatch.setattr(stt.WsConnection, "close", observe_close)

    class Handler(stt.SidecarHandler):
        def _call_loop(self, ws):
            try:
                super()._call_loop(ws)
            finally:
                finished.set()

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        ws = connect(
            f"ws://127.0.0.1:{server.server_port}/call",
            additional_headers={"X-Voice-Call-Token": "private-fixture"},
        )
        ws.send(json.dumps({"type": "hello", "channel": "#voice", "mime": "audio/wav"}))
        assert entered.wait(2)
        ws.close()
        assert closed.wait(2), "EOF must be observed while upstream is blocked"
        release.set()
        assert finished.wait(2)
        if phase == "status":
            assert actions == []
        else:
            assert [action["action"] for action in actions] == ["start", "end"]
            assert actions[0]["call_id"] == actions[1]["call_id"]
            assert actions[0]["call_id"].startswith("call-")
        assert not state.calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
