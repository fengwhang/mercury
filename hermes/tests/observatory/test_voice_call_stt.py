"""STT sidecar: WS codec vectors, token gate, MIRC proxy failure shape."""

from __future__ import annotations

import io
import socket
import struct
from types import SimpleNamespace

from observatory.voice_call_stt import (
    SidecarState,
    WsConnection,
    build_parser,
    mirc_request,
    ws_accept_key,
    ws_decode_frame,
    ws_encode_frame,
)


def test_ws_accept_known_vector() -> None:
    assert ws_accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def _masked_client_frame(payload: bytes) -> bytes:
    mask = b"\x01\x02\x03\x04"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return b"\x81" + bytes([0x80 | len(payload)]) + mask + masked


def test_ws_roundtrip_text() -> None:
    client, server = socket.socketpair()
    try:
        client.sendall(_masked_client_frame(b'{"type":"ping"}'))
        fin, opcode, payload = ws_decode_frame(server.makefile("rb"))
        assert (fin, opcode, payload) == (True, 0x1, b'{"type":"ping"}')
        server.sendall(ws_encode_frame(b'{"type":"pong"}'))
        header = client.recv(2)
        assert header[0] == 0x81 and header[1] == len(b'{"type":"pong"}')
        assert client.recv(16) == b'{"type":"pong"}'
    finally:
        client.close()
        server.close()


def test_ws_large_server_frame_uses_extended_length() -> None:
    payload = b"x" * 70000
    frame = ws_encode_frame(payload)
    assert frame[1] == 127
    (length,) = struct.unpack("!Q", frame[2:10])
    assert length == 70000


def test_ws_connection_send_json() -> None:
    client, server = socket.socketpair()
    try:
        WsConnection(server).send_json({"type": "ready"})
        header = client.recv(2)
        assert header[0] == 0x81
    finally:
        client.close()
        server.close()


def test_mirc_request_unreachable_never_raises() -> None:
    result = mirc_request("http://127.0.0.1:1", "/api/voice-call/status?channel=%23x", None, timeout=0.5)
    assert result.get("ok") is False
    assert "unreachable" in str(result.get("error", ""))

def _handler(path: str, token_header: str = "") -> SimpleNamespace:
    # /stt/health bypasses the token gate at the handler level; the gate
    # itself is path-blind and denies missing tokens everywhere else.
    return SimpleNamespace(
        path=path,
        headers={"X-Voice-Call-Token": token_header},
    )


def test_token_gate() -> None:
    gated_health = SidecarState(mirc_url="http://m:8000", stt_config={}, token="s3cret")
    assert gated_health.check_token(_handler("/stt/health")) is False
    gated = SidecarState(mirc_url="http://m:8000", stt_config={}, token="s3cret")
    assert gated.check_token(_handler("/call/action")) is False
    assert gated.check_token(_handler("/call/action?token=s3cret")) is False
    assert gated.check_token(_handler("/call/action", "s3cret")) is True
    assert gated.check_token(_handler("/call/action", "Bearer s3cret")) is False
    open_state = SidecarState(mirc_url="http://m:8000", stt_config={})
    assert open_state.check_token(_handler("/call/action")) is False


def test_parser_defaults_have_no_localhost_urls() -> None:
    args = build_parser().parse_args([])
    assert args.mirc_url == ""
    assert args.host == "127.0.0.1"
    assert args.port == 8765

def test_send_failure_still_shuts_down_socket():
    class BrokenConnection:
        shutdowns = 0
        closes = 0

        def sendall(self, _frame):
            raise OSError("peer is gone")

        def shutdown(self, _how):
            self.shutdowns += 1

        def close(self):
            self.closes += 1

    conn = BrokenConnection()
    ws = WsConnection(conn, io.BytesIO())
    ws.send_json({"type": "ready"})
    ws.close()
    assert conn.shutdowns >= 1
    assert conn.closes >= 1

def test_close_interrupts_a_blocked_socket_writer():
    import threading

    entered, released, closed = threading.Event(), threading.Event(), threading.Event()

    class BackpressuredConnection:
        def sendall(self, _frame, *_flags):
            entered.set()
            released.wait(5)
            raise OSError("socket shut down")

        def shutdown(self, _how):
            released.set()

        def close(self):
            closed.set()

    ws = WsConnection(BackpressuredConnection(), io.BytesIO())
    sender = threading.Thread(target=ws.send_json, args=({"type": "audio"},), daemon=True)
    closer = threading.Thread(target=ws.close, daemon=True)
    sender.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert closed.wait(0.5)
    finally:
        released.set()
        sender.join(2)
        closer.join(2)


def test_close_delivers_ended_after_finishing_writer_releases_lock():
    import json
    import threading

    attempted = threading.Event()
    lock = threading.Lock()

    class ObservedLock:
        def acquire(self, blocking=True, timeout=-1):
            if not blocking:
                acquired = lock.acquire(blocking=False)
                attempted.set()
                return acquired
            attempted.set()
            return lock.acquire(timeout=timeout)

        def release(self):
            lock.release()

    client, server = socket.socketpair()
    client.settimeout(1)
    ws = WsConnection(server)
    ws.lock = ObservedLock()
    # A writer has completed its send, but has not released its lock yet.
    lock.acquire()
    closer = threading.Thread(
        target=ws.close, kwargs={"message": {"type": "ended"}}, daemon=True,
    )
    closer.start()
    try:
        assert attempted.wait(1)
        lock.release()
        closer.join(1)
        assert not closer.is_alive()
        expected = ws_encode_frame(json.dumps({"type": "ended"}).encode())
        expected += ws_encode_frame(struct.pack("!H", 1000), 0x8)
        with client.makefile("rb") as frames:
            assert frames.read(len(expected)) == expected
    finally:
        if lock.locked():
            lock.release()
        closer.join(1)
        client.close()
        server.close()


def test_explicit_sidecar_home_overrides_inherited_mercury_config(tmp_path, monkeypatch):
    import json
    from observatory import voice_call_stt as stt

    home = tmp_path / "voice-home"
    home.mkdir()
    (home / ".env").write_text("VOICE_CALL_SIDECAR_TOKEN=fixture-sidecar\nVOICE_CALL_MIRC_TOKEN=fixture-mirc\n")
    (home / "config.yaml").write_text(json.dumps({
        "stt": {"provider": "qwen3-asr"},
        "voice_call": {"mirc_host_url": "http://explicit-mirc:8123"},
    }))
    inherited = tmp_path / "inherited.yaml"
    inherited.write_text(json.dumps({
        "stt": {"provider": "openai"},
        "voice_call": {"mirc_host_url": "http://wrong-mirc:9123"},
    }))
    monkeypatch.setenv("MERCURY_CONFIG", str(inherited))
    served = []

    class Server:
        def __init__(self, address, handler):
            served.append((handler.state.mirc_url, handler.state.stt_config["provider"]))

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(stt, "ThreadingHTTPServer", Server)
    assert stt.main(["--home", str(home)]) == 0
    assert served == [("http://explicit-mirc:8123", "qwen3-asr")]
