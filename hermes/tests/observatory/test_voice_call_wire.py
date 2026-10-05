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
            self.respond({"ok": True, "allowed": True, "engine": "hermes"})

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

    def transcribe(audio, mime, cfg):
        chunks.append((audio, mime))
        return {"success": True, "transcript": "hello"}

    monkeypatch.setattr(stt, "transcribe_chunk", transcribe)
    with connect(url) as ws:
        ws.send(json.dumps({"type": "hello", "channel": "#chat", "mime": "audio/mp4"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ready"
        ws.send(b"independent-mp4-segment")
        assert json.loads(ws.recv(timeout=2))["text"] == "hello"
        assert chunks == [(b"independent-mp4-segment", "audio/mp4")]
        ws.send(json.dumps({"type": "tts", "text": "agent reply", "token": "reply-1"}))
        assert json.loads(ws.recv(timeout=2))["dataUrl"].startswith("data:audio/mpeg;")
        ws.send(json.dumps({"type": "hangup"}))
        assert json.loads(ws.recv(timeout=2))["type"] == "ended"
    assert not state.calls
    assert all(auth == "Bearer service-secret" for _, auth, _ in requests)
    assert requests[-1][2] == {"action": "end", "channel": "#chat"}


def test_voice_service_auth_cannot_override_engine_or_admin(monkeypatch):
    from fastapi.testclient import TestClient
    from mercury_cli import web_server

    monkeypatch.setenv("VOICE_CALL_MIRC_TOKEN", "service-secret")
    monkeypatch.setattr(web_server.app.state, "auth_required", True, raising=False)
    monkeypatch.setattr(vc, "resolve_channel_engine", lambda _channel: "omp")
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer service-secret"}
    refused = client.post("/api/voice-call/call", headers=headers,
                          json={"action": "start", "channel": "#coder", "engine": "hermes"})
    assert refused.status_code == 409
    assert "OMP" in refused.json()["detail"]
    assert client.get("/api/voice-call/status", headers=headers).status_code == 200
    assert client.get("/api/voice-call/status", follow_redirects=False).status_code != 200
    assert client.get("/api/config", headers=headers, follow_redirects=False).status_code != 200


def test_hangup_and_ping_are_responsive_while_transcribing(sidecar, monkeypatch):
    url, _requests, state = sidecar
    entered, release = threading.Event(), threading.Event()

    def slow_transcription(*args):
        entered.set()
        assert release.wait(5)
        return {"success": True, "transcript": "late result must not steer"}

    monkeypatch.setattr(stt, "transcribe_chunk", slow_transcription)
    try:
        with connect(url) as ws:
            ws.send(json.dumps({"type": "hello", "channel": "#chat"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "ready"
            ws.send(b"slow-audio")
            assert entered.wait(2)
            ws.send(json.dumps({"type": "ping"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "pong"
            ws.send(json.dumps({"type": "hangup"}))
            assert json.loads(ws.recv(timeout=2))["type"] == "ended"
            assert not state.calls
    finally:
        release.set()
