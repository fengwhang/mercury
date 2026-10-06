"""Exercise sidecar/browser traffic and dashboard auth without live services."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from websockets.sync.client import connect

from observatory import voice_call as vc, voice_call_stt as stt


@pytest.fixture
def sidecar():
    requests = []

    class Mirc(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization"), None))
            self.respond({"ok": True, "allowed": True, "engine": "hermes", "agent_name": "Gaia"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers.get("Authorization"), body))
            self.respond({"ok": True, "data_url": "data:audio/mpeg;base64,AA=="})

    mirc = ThreadingHTTPServer(("127.0.0.1", 0), Mirc)
    mirc_thread = threading.Thread(target=mirc.serve_forever, daemon=True)
    mirc_thread.start()
    state = stt.SidecarState(f"http://127.0.0.1:{mirc.server_port}", {},
                            token="browser-secret", mirc_token="service-secret")

    class Handler(stt.SidecarHandler):
        pass

    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"ws://127.0.0.1:{server.server_port}/call?token=browser-secret", requests, state
    finally:
        server.shutdown()
        mirc.shutdown()
        server.server_close()
        mirc.server_close()
        thread.join(2)
        mirc_thread.join(2)


def test_browser_codec_transcript_and_speech_roundtrip(sidecar, monkeypatch):
    url, requests, state = sidecar
    chunks = []
    end_completed = threading.Event()
    request = state.request

    def track_end(path, payload=None, **kwargs):
        result = request(path, payload, **kwargs)
        if payload and payload.get("action") == "end":
            end_completed.set()
        return result

    monkeypatch.setattr(state, "request", track_end)

    def transcribe(audio, mime, cfg):
        chunks.append((audio, mime))
        return {"success": True, "transcript": "hello"}

    monkeypatch.setattr(stt, "transcribe_chunk", transcribe)
    with connect(url) as ws:
        ws.send(json.dumps({"type": "hello", "channel": "#chat", "mime": "audio/mp4"}))
        ready = json.loads(ws.recv(timeout=2))
        assert ready["type"] == "ready"
        assert ready.get("agentName") == "Gaia"
        ws.send(b"independent-mp4-segment")
        assert json.loads(ws.recv(timeout=2))["text"] == "hello"
        assert chunks == [(b"independent-mp4-segment", "audio/mp4")]
        ws.send(json.dumps({"type": "tts", "text": "agent reply", "token": "reply-1"}))
        assert json.loads(ws.recv(timeout=2))["dataUrl"].startswith("data:audio/mpeg;")
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ended"
    assert not state.calls
    assert end_completed.wait(2)
    assert all(auth == "Bearer service-secret" for _, auth, _ in requests)
    ended = requests[-1][2]
    assert ended["action"] == "end" and ended["channel"] == "#chat"
    assert ended["call_id"] == next(body["call_id"] for _, _, body in requests
                                   if body and body.get("action") == "start")


def test_voice_service_auth_cannot_override_engine_or_admin(monkeypatch):
    from fastapi.testclient import TestClient
    from mercury_cli import web_server

    monkeypatch.setenv("VOICE_CALL_MIRC_TOKEN", "service-secret")
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(vc, "resolve_channel_agent", lambda _channel: {"engine": "omp", "name": "Coder"})
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer service-secret"}
    refused = client.post("/api/voice-call/call", headers=headers,
                          json={"action": "start", "channel": "#coder", "engine": "hermes"})
    assert refused.status_code == 409
    assert "OMP" in refused.json()["detail"]
    assert client.get("/api/voice-call/status", headers=headers).status_code == 200
    assert client.get("/api/voice-call/status", follow_redirects=False).status_code != 200
    assert client.get("/api/config", headers=headers, follow_redirects=False).status_code != 200


@pytest.mark.parametrize("provider", ["stt", "tts"])
def test_hangup_and_ping_are_responsive_while_provider_blocked(sidecar, monkeypatch, provider):
    url, _requests, state = sidecar
    entered, release = threading.Event(), threading.Event()

    def slow_transcription(*args):
        entered.set()
        assert release.wait(5)
        return {"success": True, "transcript": "late result must not steer"}

    request = state.request

    def slow_speech(path, payload=None, **kwargs):
        if path == "/api/audio/speak":
            entered.set()
            assert release.wait(5)
            return {"ok": True, "data_url": "data:audio/mpeg;base64,AA=="}
        return request(path, payload, **kwargs)

    monkeypatch.setattr(stt, "transcribe_chunk", slow_transcription)
    monkeypatch.setattr(state, "request", slow_speech)
    try:
        with connect(url) as ws:
            ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "ready"
            ws.send(b"slow-audio" if provider == "stt" else
                    json.dumps({"type": "tts", "text": "slow reply", "token": "reply"}))
            assert entered.wait(2)
            ws.send(json.dumps({"type": "ping"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "pong"
            ws.send(json.dumps({"type": "hangup"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "ended"
            assert not state.calls
    finally:
        release.set()

def test_muted_audio_never_reaches_stt(sidecar, monkeypatch):
    url, _requests, _state = sidecar
    chunks = []
    monkeypatch.setattr(stt, "transcribe_chunk", lambda audio, *_: (
        chunks.append(audio) or {"success": True, "transcript": "should not arrive"}
    ))
    with connect(url) as ws:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(json.dumps({"type": "mute", "muted": True}))
        assert json.loads(ws.recv(timeout=2))["type"] == "muted"
        ws.send(b"muted-microphone")
        ws.send(json.dumps({"type": "ping"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "pong"
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ended"
    assert chunks == []

def test_hangup_closes_socket_before_blocked_registry_end(sidecar, monkeypatch):
    from websockets.exceptions import ConnectionClosedOK

    url, _requests, state = sidecar
    entered, release = threading.Event(), threading.Event()
    request = state.request

    def slow_end(path, payload=None, **kwargs):
        if payload and payload.get("action") == "end":
            entered.set()
            assert release.wait(5)
        return request(path, payload, **kwargs)

    monkeypatch.setattr(state, "request", slow_end)
    try:
        with connect(url) as ws:
            ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "ready"
            ws.send(json.dumps({"type": "hangup"}))
            assert entered.wait(2)
            assert not state.calls
            assert json.loads(ws.recv(timeout=0.5))["type"] == "ended"
            with pytest.raises(ConnectionClosedOK):
                ws.recv(timeout=0.5)
    finally:
        release.set()


@pytest.mark.parametrize("failure", ["ready", "worker-start"])
def test_setup_failure_cleans_started_registry(monkeypatch, failure):
    state = stt.SidecarState("http://unused", {})
    actions = []
    monkeypatch.setattr(state, "request", lambda _path, payload=None, **_: (
        actions.append(payload) or {"ok": True, "allowed": True, "engine": "hermes"}
    ))
    handler = object.__new__(stt.SidecarHandler)
    handler.state = state

    class BrokenSocket:
        def recv_message(self):
            return "text", b'{"type":"hello","channel":"#chat"}'

        def send_json(self, message):
            if failure == "ready":
                raise OSError("disconnected before ready")
        def close(self, **_):
            pass

    if failure == "worker-start":
        class BrokenThread:
            def __init__(self, **_):
                pass

            def start(self):
                raise OSError("cannot start worker")

        monkeypatch.setattr(stt.threading, "Thread", BrokenThread)
    with pytest.raises(OSError):
        handler._call_loop(BrokenSocket())
    assert not state.calls
    assert [p["action"] for p in actions if p] == ["start", "end"]

def test_simultaneous_socket_owners_do_not_end_each_other(monkeypatch):
    from fastapi.testclient import TestClient
    from mercury_cli import web_server

    monkeypatch.setenv("VOICE_CALL_MIRC_TOKEN", "service-secret")
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(vc, "resolve_channel_agent", lambda _: {"engine": "hermes", "name": "Gaia"})
    store = vc.VoiceCallStore()
    monkeypatch.setattr(vc, "default_store", lambda: store)
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer service-secret"}

    def action(kind, owner):
        response = client.post("/api/voice-call/call", headers=headers, json={
            "action": kind, "channel": "#chat", "call_id": owner,
        })
        assert response.status_code == 200
        return response.json()

    action("start", "browser-a")
    action("start", "browser-b")
    action("end", "browser-a")
    assert store.status("#chat")["active"]
    action("mute", "browser-b")
    assert store.status("#chat")["muted"]
    # Delayed teardown from A must not affect B, even on a reused channel.
    assert not action("end", "browser-a")["ended"]
    assert store.status("#chat")["active"]
    action("end", "browser-b")
    assert not store.status("#chat")["active"]

def test_hangup_remains_responsive_during_mute_registry_request(sidecar, monkeypatch):
    url, _requests, state = sidecar
    entered, release = threading.Event(), threading.Event()
    request = state.request

    def blocked_mute(path, payload=None, **kwargs):
        if payload and payload.get("action") == "mute":
            entered.set()
            release.wait(5)
        return request(path, payload, **kwargs)

    monkeypatch.setattr(state, "request", blocked_mute)
    ws = connect(url)
    try:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(json.dumps({"type": "mute", "muted": True}))
        assert entered.wait(2)
        assert json.loads(ws.recv(timeout=0.5))["type"] == "muted"
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=0.5))["type"] == "ended"
        assert not state.calls
    finally:
        release.set()
        ws.close()


def test_mute_invalidates_queued_and_inflight_audio_after_unmute(sidecar, monkeypatch):
    url, _requests, _state = sidecar
    entered, release = threading.Event(), threading.Event()
    chunks = []

    def transcribe(audio, *_):
        chunks.append(audio)
        if audio == b"inflight":
            entered.set()
            release.wait(5)
        return {"success": True, "transcript": audio.decode()}

    monkeypatch.setattr(stt, "transcribe_chunk", transcribe)
    ws = connect(url)
    try:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(b"inflight")
        assert entered.wait(2)
        ws.send(b"queued-before-mute")
        ws.send(json.dumps({"type": "mute", "muted": True}))
        assert json.loads(ws.recv(timeout=2))["type"] == "muted"
        ws.send(b"queued-while-muted")
        ws.send(json.dumps({"type": "mute", "muted": False}))
        assert json.loads(ws.recv(timeout=2))["type"] == "muted"
        release.set()
        ws.send(b"fresh")
        assert json.loads(ws.recv(timeout=2))["text"] == "fresh"
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ended"
        assert chunks == [b"inflight", b"fresh"]
    finally:
        release.set()
        ws.close()

def test_worker_exception_closes_call_and_registry(sidecar, monkeypatch):
    from websockets.exceptions import ConnectionClosed

    url, requests, state = sidecar
    ended = threading.Event()
    request = state.request

    def track_end(path, payload=None, **kwargs):
        result = request(path, payload, **kwargs)
        if payload and payload.get("action") == "end":
            ended.set()
        return result

    monkeypatch.setattr(state, "request", track_end)

    def broken_stt(*_):
        raise RuntimeError("provider worker broke")

    monkeypatch.setattr(stt, "transcribe_chunk", broken_stt)
    ws = connect(url)
    try:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(b"audio")
        assert json.loads(ws.recv(timeout=0.5))["type"] == "error"
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=0.5)
        assert ended.wait(2)
        assert not state.calls
        assert requests[-1][2]["action"] == "end"
    finally:
        ws.close()

@pytest.mark.asyncio
async def test_cancelled_synthesis_removes_late_generated_audio(tmp_path, monkeypatch):
    import asyncio
    import contextlib
    from mercury_cli import web_server
    from mercury_cli.web_models import TTSSpeakRequest
    from tools import tts_tool

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    audio = tmp_path / "late.mp3"

    def synthesize(_text):
        entered.set()
        release.wait(5)
        audio.write_bytes(b"fake-audio")
        finished.set()
        return {"success": True, "file_path": str(audio)}

    monkeypatch.setattr(tts_tool, "text_to_speech_tool", synthesize)
    monkeypatch.setattr(web_server, "_config_profile_scope", lambda _: contextlib.nullcontext())
    task = asyncio.create_task(web_server.speak_text(TTSSpeakRequest(text="hello")))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        # Let the executor's completion callback perform cancellation cleanup.
        for _ in range(100):
            if not audio.exists():
                break
            await asyncio.sleep(0.01)
        assert not audio.exists()
    finally:
        release.set()


def test_mute_ack_prevents_already_completed_transcript_emission(sidecar, monkeypatch):
    url, _requests, _state = sidecar
    entered, release = threading.Event(), threading.Event()

    class PausedTranscript:
        def __str__(self):
            entered.set()
            release.wait(5)
            return "stale transcript"

    monkeypatch.setattr(stt, "transcribe_chunk", lambda *_: {
        "success": True, "transcript": PausedTranscript(),
    })
    ws = connect(url)
    try:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(b"audio")
        assert entered.wait(2)
        ws.send(json.dumps({"type": "mute", "muted": True}))
        assert json.loads(ws.recv(timeout=2))["type"] == "muted"
        release.set()
        ws.send(json.dumps({"type": "ping"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "pong"
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ended"
    finally:
        release.set()
        ws.close()

def test_status_exposes_registered_name_not_room_slug(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from mercury_cli import web_server
    from observatory import rooms, state

    database = tmp_path / "state.db"
    with state.ObservatoryState(database) as tree:
        tree.add_node("test-agent", engine="hermes", name="Gaia the companion",
                      slug="conversation-42", mxid="test-agent", session_ref="test-agent")
        tree.set_room_id("test-agent", "#conversation-42")
    monkeypatch.setattr(rooms, "get_room_manager", lambda: None)
    monkeypatch.setattr(state, "default_state_db_path", lambda: database)
    monkeypatch.setenv("VOICE_CALL_MIRC_TOKEN", "service-secret")
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    client = TestClient(web_server.app)
    response = client.get("/api/voice-call/status", params={"channel": "#conversation-42"},
                          headers={"Authorization": "Bearer service-secret"})
    assert response.status_code == 200
    assert response.json()["engine"] == "hermes"
    assert response.json().get("agent_name") == "Gaia the companion"

@pytest.mark.parametrize("control", ["websocket-ping", "ping", "mute"])
def test_control_before_hangup_cannot_pin_reader_behind_tts_writer(sidecar, monkeypatch, control):
    from websockets.exceptions import ConnectionClosed

    url, _requests, state = sidecar
    entered, release, ended = threading.Event(), threading.Event(), threading.Event()
    send_json = stt.WsConnection.send_json
    request = state.request

    def blocked_writer(ws, message, **kwargs):
        if message.get("type") == "audio":
            with ws.lock:
                entered.set()
                release.wait(5)
        return send_json(ws, message, **kwargs)

    def track_end(path, payload=None, **kwargs):
        result = request(path, payload, **kwargs)
        if payload and payload.get("action") == "end":
            ended.set()
        return result

    monkeypatch.setattr(stt.WsConnection, "send_json", blocked_writer)
    monkeypatch.setattr(state, "request", track_end)
    ws = connect(url)
    try:
        ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(json.dumps({"type": "tts", "text": "reply", "token": "blocked"}))
        assert entered.wait(2)
        if control == "websocket-ping":
            ws.ping(b"probe")
        else:
            ws.send(json.dumps({"type": control, "muted": True}))
        ws.send(json.dumps({"type": "hangup"}))
        # An undrainable data frame cannot interleave an acknowledgement.
        # Abort honestly rather than leave the reader and registry pinned.
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=0.5)
        assert ended.wait(2)
        assert not state.calls
    finally:
        release.set()
        ws.close()