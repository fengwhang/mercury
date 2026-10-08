"""Real HTTP errors remain responses, not transport failures."""

import json
import io
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

import pytest

from observatory.voice_call_stt import mirc_request


@contextmanager
def error_server(status, body):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_http_503_preserves_public_provider_detail():
    detail = "Configure TTS in Mercury Setup on the MIRC host; no cloud fallback"
    with error_server(503, json.dumps({"detail": detail}).encode()) as url:
        result = mirc_request(url, "/api/audio/speak")
    assert result == {"ok": False, "status": 503, "error": detail}


@pytest.mark.parametrize("field,message", [
    ("detail", "Configure STT in Mercury Setup on the mLounge host"),
    ("error", "Voice calls disabled in Mercury Setup"),
])
def test_public_error_fields_are_not_one_exact_tts_string(field, message):
    with error_server(503, json.dumps({field: message}).encode()) as url:
        assert mirc_request(url, "/api/voice-call/status") == {
            "ok": False, "status": 503, "error": message,
        }


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_do_not_echo_configuration_or_credentials(status, caplog):
    secret = "fake-provider-canary-do-not-echo"
    body = json.dumps({"detail": secret, "error": "Configure TTS in Mercury Setup on the MIRC host"}).encode()
    with error_server(status, body) as url:
        result = mirc_request(url, "/api/audio/speak", token=secret)
    assert result == {"ok": False, "status": status, "error": "request failed"}
    assert secret not in caplog.text


@pytest.mark.parametrize("body", [
    b"<html>private stack https://host/?token=canary</html>",
    b'{"detail":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}",
    b"{", b"\xff", b"[]",
    b'{"detail":["private-canary"]}',
    b'{"detail":{"error":"private-canary"}}',
    b'{"detail":"fake-provider-canary-do-not-echo"}',
    json.dumps({"detail": "x" * 513}).encode(),
    json.dumps({"detail": "Configure TTS in Mercury Setup on the MIRC host", "private": "x" * 4096}).encode(),
])
def test_unvalidated_or_oversized_body_has_safe_fallback(body, caplog):
    with error_server(503, body) as url:
        result = mirc_request(url, "/api/audio/speak")
    assert result == {"ok": False, "status": 503, "error": "request failed"}
    assert "canary" not in caplog.text


def test_transport_refusal_is_unreachable_without_echoing_url_or_token():
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        result = mirc_request(f"http://127.0.0.1:{port}", "/?token=fake-canary", timeout=0.2)
    assert result == {"ok": False, "error": "MIRC host unreachable"}


def test_http_error_read_is_bounded_and_response_is_closed(monkeypatch):
    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            assert size == 4097
            return super().read(size)

    body = BoundedBody(b"x" * 10000)
    error = HTTPError("http://private/?token=canary", 503, "private-canary", {}, body)

    class Opener:
        def open(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Opener())
    assert mirc_request("http://private", "/") == {
        "ok": False, "status": 503, "error": "request failed",
    }
    assert body.closed


def test_configured_secret_in_public_looking_message_is_not_echoed(monkeypatch):
    message = "Configure TTS in Mercury Setup on the MIRC host"
    monkeypatch.setenv("VOICE_CALL_SIDECAR_TOKEN", "Mercury")
    with error_server(503, json.dumps({"detail": message}).encode()) as url:
        assert mirc_request(url, "/")["error"] == "request failed"
