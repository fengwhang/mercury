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
    assert gated.check_token(_handler("/call/action?token=s3cret")) is True
    assert gated.check_token(_handler("/call/action", "s3cret")) is True
    assert gated.check_token(_handler("/call/action", "Bearer s3cret")) is True
    open_state = SidecarState(mirc_url="http://m:8000", stt_config={})
    assert open_state.check_token(_handler("/call/action")) is True


def test_parser_defaults_have_no_localhost_urls() -> None:
    args = build_parser().parse_args([])
    assert args.mirc_url == ""
    assert args.host == "127.0.0.1"
    assert args.port == 8765
