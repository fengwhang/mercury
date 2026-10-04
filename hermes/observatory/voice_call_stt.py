#!/usr/bin/env python3
"""Voice-call STT sidecar + signaling relay for the mLounge host.

Three-tier audio path (see docs/voice-call.md)::

  browser (getUserMedia mic / HTMLAudio speakers)
      │ WebSocket /call — audio chunks up, transcripts + TTS audio down
      ▼
  this sidecar (mLounge host) — selected ASR + relay to the MIRC host
      │ HTTP — transcripts are returned to the browser (which delivers
      │ them over the existing chat socket); TTS proxied MIRC-side
      ▼
  MIRC host — Hermes loop + ``/api/audio/speak`` TTS + ``/api/voice-call/*``

Hard rules:

- Mic capture happens ONLY in the browser. This process never opens a
  local audio device; every byte it transcribes arrived over the network.
- Playback happens ONLY in the browser. This process never plays audio;
  it forwards base64 TTS payloads back down the call socket.
- No localhost assumption: the MIRC base URL is explicit (``--mirc-url``
  or ``voice_call.mirc_host_url``) and the browser reaches this sidecar
  at an explicit URL (``voice_call.stt_sidecar_url``). Same-machine is a
  degenerate case, never a default.
- Hermes-only: the opening ``hello`` resolves the channel engine through
  the MIRC host and refuses OMP rooms before any audio flows.

Run on the mLounge host (after ``mercury setup stt`` there)::

  python -m observatory.voice_call_stt --host 0.0.0.0 --port 8765 \\
      --mirc-url http://mirc-host:8000 --token s3cret

Stdlib only — no third-party server dependencies on the mLounge host.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

logger = logging.getLogger("voice_call_stt")

REPO_HERMES = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(REPO_HERMES, "tools")):
    sys.path.insert(0, os.path.dirname(REPO_HERMES))

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE_BYTES = 32 * 1024 * 1024

CHUNK_SUFFIX_BY_MIME = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
}

HEALTH_PATH = "/stt/health"
TRANSCRIBE_PATH = "/stt/transcribe"
TTS_PROXY_PATH = "/call/tts"
ACTION_PROXY_PATH = "/call/action"
STATUS_PROXY_PATH = "/call/status"
CALL_WS_PATH = "/call"


# ---------------------------------------------------------------------------
# WebSocket framing (RFC 6455 server side, stdlib)
# ---------------------------------------------------------------------------


def ws_accept_key(client_key: str) -> str:
    """Compute Sec-WebSocket-Accept for a client key (pure)."""
    digest = hashlib.sha1((client_key.strip() + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Encode one server-to-client frame (never masked)."""
    header = bytes([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < (1 << 16):
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + payload


def _read_exact(rfile: Any, count: int) -> bytes:
    chunks = []
    remaining = count
    while remaining > 0:
        piece = rfile.read(min(remaining, 65536))
        if not piece:
            raise ConnectionError("websocket peer closed mid-frame")
        chunks.append(piece)
        remaining -= len(piece)
    return b"".join(chunks)


def ws_decode_frame(rfile: Any) -> tuple[bool, int, bytes]:
    """Read one client-to-server frame. Returns (fin, opcode, payload)."""
    header = _read_exact(rfile, 2)
    first, second = header[0], header[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _read_exact(rfile, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exact(rfile, 8))
    if length > WS_MAX_MESSAGE_BYTES:
        raise ValueError("websocket frame too large")
    mask = _read_exact(rfile, 4) if masked else b""
    payload = _read_exact(rfile, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WsConnection:
    """Blocking server-side connection over an hijacked HTTP socket."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.rfile = conn.makefile("rb")
        self.lock = threading.Lock()
        self.closed = False

    def send_json(self, message: Dict[str, Any]) -> None:
        raw = json.dumps(message).encode("utf-8")
        with self.lock:
            if self.closed:
                return
            try:
                self.conn.sendall(ws_encode_frame(raw, 0x1))
            except OSError:
                self.closed = True

    def recv_message(self) -> tuple[str, bytes]:
        """Next complete message as (kind, payload): text|binary|close."""
        fragments: list[bytes] = []
        text_mode: Optional[bool] = None
        while True:
            fin, opcode, payload = ws_decode_frame(self.rfile)
            if opcode == 0x8:  # close
                return "close", payload
            if opcode == 0x9:  # ping
                with self.lock:
                    try:
                        self.conn.sendall(ws_encode_frame(payload, 0xA))
                    except OSError:
                        pass
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                text_mode = opcode == 0x1
            elif opcode != 0x0:
                raise ValueError(f"unsupported websocket opcode {opcode}")
            fragments.append(payload)
            if fin:
                kind = "text" if text_mode else "binary"
                return kind, b"".join(fragments)

    def close(self, code: int = 1000, reason: str = "") -> None:
        payload = struct.pack("!H", code) + reason.encode("utf-8")
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                self.conn.sendall(ws_encode_frame(payload, 0x8))
            except OSError:
                pass
            try:
                self.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# MIRC host proxy (urllib, stdlib)
# ---------------------------------------------------------------------------


def mirc_request(
    mirc_url: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """POST/GET JSON against the MIRC host. Never raises: errors → dict."""
    url = mirc_url.rstrip("/") + path
    try:
        data = None
        headers = {"Content-Type": "application/json"}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        parsed = json.loads(body) if body.strip() else {}
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": "bad MIRC response"}
    except Exception as exc:
        return {"ok": False, "error": f"MIRC host unreachable ({exc})"}


# ---------------------------------------------------------------------------
# STT dispatch with in-memory config overlay (no local mic, ever)
# ---------------------------------------------------------------------------


def load_sidecar_stt_config(overlays: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Load the mLounge host's stt config with CLI flag overlays applied."""
    from tools.transcription_tools import _load_stt_config

    stt_config = _load_stt_config()
    if not isinstance(stt_config, dict):
        stt_config = {}
    else:
        stt_config = dict(stt_config)
    overlays = overlays or {}
    provider = (overlays.get("provider") or "").strip().lower()
    if provider:
        stt_config["provider"] = provider
        section = dict(stt_config.get(provider) or {})
        if overlays.get("model"):
            section["model"] = overlays["model"]
        if overlays.get("language"):
            section["language"] = overlays["language"]
        if overlays.get("endpoint"):
            section["endpoint"] = overlays["endpoint"]
        stt_config[provider] = section
        providers = dict(stt_config.get("providers") or {})
        if provider in providers and isinstance(providers[provider], dict):
            entry = dict(providers[provider])
            if overlays.get("model"):
                entry["model"] = overlays["model"]
            if overlays.get("language"):
                entry["language"] = overlays["language"]
            providers[provider] = entry
            stt_config["providers"] = providers
    return stt_config


def active_stt_provider(stt_config: Dict[str, Any]) -> str:
    """Resolved provider name for status reporting (never raises)."""
    try:
        from tools.transcription_tools import _get_provider

        return str(_get_provider(stt_config))
    except Exception as exc:
        return f"unavailable ({exc})"

def transcribe_chunk(
    audio_bytes: bytes,
    mime: str,
    stt_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Transcribe one browser-sourced audio chunk. No audio device touched.

    Mirrors ``tools.transcription_tools._transcribe_prepared_audio`` guards
    (file safety, validation, non-local size cap, cloud silence trim) while
    serving the overlay config instead of the on-disk one.
    """
    if not audio_bytes:
        return {"success": False, "transcript": "", "error": "empty audio chunk"}
    try:
        from agent.file_safety import get_read_block_error
        from tools import transcription_tools as tt
    except Exception as exc:
        return {"success": False, "transcript": "", "error": f"STT backend unavailable: {exc}"}
    suffix = CHUNK_SUFFIX_BY_MIME.get((mime or "").split(";")[0].strip().lower(), ".webm")
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="voice-call-chunk-", suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        blocked = get_read_block_error(tmp_path)
        if blocked:
            return {"success": False, "transcript": "", "error": blocked}
        error = tt._validate_audio_file(tmp_path, enforce_size_limit=False)
        if error:
            return error
        provider = tt._get_provider(stt_config)
        if not tt._is_local_stt_provider(provider, stt_config):
            error = tt._validate_audio_file_size(__import__("pathlib").Path(tmp_path))
            if error:
                return error
        file_path = tmp_path
        trim_dir: Optional[str] = None
        try:
            cloud_providers = getattr(tt, "CLOUD_STT_PROVIDERS", frozenset())
        except Exception:
            cloud_providers = frozenset()
        if provider in cloud_providers:
            try:
                trimmed = tt._trim_silence_for_cloud_stt(tmp_path, stt_config)
            except Exception:
                trimmed = None
            if trimmed:
                file_path = trimmed
                trim_dir = os.path.dirname(trimmed)
        try:
            return tt._dispatch_stt_provider(
                file_path, provider, stt_config, None, "voice-call",
            )
        finally:
            if trim_dir:
                shutil.rmtree(trim_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("chunk transcription failed")
        return {"success": False, "transcript": "", "error": f"transcription failed: {exc}"}
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# HTTP + WebSocket server
# ---------------------------------------------------------------------------


class SidecarState:
    def __init__(
        self,
        mirc_url: str,
        stt_config: Dict[str, Any],
        token: str = "",
    ) -> None:
        self.mirc_url = mirc_url.rstrip("/")
        self.stt_config = stt_config
        self.token = token
        self.calls: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.call_seq = 0

    def check_token(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.token:
            return True
        query = urllib.parse.urlparse(handler.path).query
        params = urllib.parse.parse_qs(query)
        presented = ""
        for values in (params.get("token") or []):
            presented = values
        if not presented:
            presented = (handler.headers.get("X-Voice-Call-Token") or "").strip()
        if presented.startswith("Bearer "):
            presented = presented[len("Bearer "):].strip()
        return presented == self.token

    def next_call_id(self) -> str:
        with self.lock:
            self.call_seq += 1
            return f"call-{int(time.time())}-{self.call_seq}"


class SidecarHandler(BaseHTTPRequestHandler):
    state: SidecarState
    server_version = "VoiceCallSTT/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Voice-Call-Token")

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802 — handler naming convention
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == HEALTH_PATH:
            provider = active_stt_provider(self.state.stt_config)
            self._send_json({"ok": True, "provider": provider, "path": "browser->mLounge-STT"})
            return
        if parsed.path == STATUS_PROXY_PATH:
            if not self.state.check_token(self):
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            query = urllib.parse.parse_qs(parsed.query)
            channel = (query.get("channel") or [""])[0]
            result = mirc_request(
                self.state.mirc_url,
                f"/api/voice-call/status?channel={urllib.parse.quote(channel)}",
                None,
                timeout=10.0,
            )
            self._send_json(result)
            return
        if parsed.path == CALL_WS_PATH and (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._serve_call_socket()
            return
        self._send_json({"ok": False, "error": "not found"}, 404)

    def _read_json_body(self, limit: int = 32 * 1024 * 1024) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return {}
        try:
            raw = self.rfile.read(length)
        except OSError:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in (TRANSCRIBE_PATH, TTS_PROXY_PATH, ACTION_PROXY_PATH):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not self.state.check_token(self):
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        body = self._read_json_body()
        if parsed.path == TRANSCRIBE_PATH:
            self._handle_transcribe(body)
        elif parsed.path == TTS_PROXY_PATH:
            self._handle_tts_proxy(body)
        else:
            self._handle_action_proxy(body)

    def _handle_transcribe(self, body: Dict[str, Any]) -> None:
        try:
            audio = base64.b64decode(str(body.get("audio_base64") or ""), validate=True)
        except Exception:
            self._send_json({"ok": False, "error": "audio_base64 is not valid base64"}, 400)
            return
        mime = str(body.get("mime") or "audio/webm")
        result = transcribe_chunk(audio, mime, self.state.stt_config)
        if result.get("success"):
            self._send_json({
                "ok": True,
                "transcript": str(result.get("transcript") or ""),
                "provider": str(result.get("provider") or ""),
            })
        else:
            self._send_json({"ok": False, "error": str(result.get("error") or "transcription failed")}, 502)

    def _handle_tts_proxy(self, body: Dict[str, Any]) -> None:
        text = (body.get("text") or "").strip() if isinstance(body.get("text"), str) else ""
        if not text:
            self._send_json({"ok": False, "error": "text is required"}, 400)
            return
        result = mirc_request(
            self.state.mirc_url, "/api/audio/speak", {"text": text}, timeout=90.0,
        )
        self._send_json(result, 200 if result.get("ok") else 502)

    def _handle_action_proxy(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "").strip().lower()
        channel = str(body.get("channel") or "").strip()
        if action not in ("start", "end", "mute", "unmute") or not channel:
            self._send_json({"ok": False, "error": "action must be start|end|mute|unmute with channel"}, 400)
            return
        result = mirc_request(
            self.state.mirc_url,
            "/api/voice-call/call",
            {"action": action, "channel": channel},
            timeout=10.0,
        )
        self._send_json(result, 200 if result.get("ok") else 502)

    # -- call socket ----------------------------------------------------

    def _serve_call_socket(self) -> None:
        key = (self.headers.get("Sec-WebSocket-Key") or "").strip()
        if not key:
            self._send_json({"ok": False, "error": "missing websocket key"}, 400)
            return
        if not self.state.check_token(self):
            self.send_response(401)
            self._cors()
            self.end_headers()
            return
        accept = ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        conn = self.connection
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ws = WsConnection(conn)
        # Detach from http.server bookkeeping: the frame loop owns the
        # socket now; suppress the handler's finish/close dance.
        self.close_connection = True
        try:
            self._call_loop(ws)
        except (ConnectionError, ValueError, OSError) as exc:
            logger.info("call socket closed: %s", exc)
        except Exception:
            logger.exception("call loop failed")
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _call_loop(self, ws: WsConnection) -> None:
        kind, payload = ws.recv_message()
        if kind != "text":
            ws.send_json({"type": "error", "message": "first frame must be a hello object"})
            return
        try:
            hello = json.loads(payload.decode("utf-8"))
        except Exception:
            hello = {}
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            ws.send_json({"type": "error", "message": "first frame must be {type: hello, channel}"})
            return
        channel = str(hello.get("channel") or "").strip()
        if not channel:
            ws.send_json({"type": "error", "message": "hello.channel is required"})
            return
        status = mirc_request(
            self.state.mirc_url,
            f"/api/voice-call/status?channel={urllib.parse.quote(channel)}",
            None,
            timeout=10.0,
        )
        engine = str(status.get("engine") or "hermes")
        if not status.get("ok") or not status.get("allowed", True):
            ws.send_json({
                "type": "refused",
                "reason": str(status.get("reason") or status.get("error") or "call not allowed"),
            })
            return
        started = mirc_request(
            self.state.mirc_url,
            "/api/voice-call/call",
            {"action": "start", "channel": channel, "engine": engine},
            timeout=10.0,
        )
        if not started.get("ok"):
            ws.send_json({
                "type": "refused",
                "reason": str(started.get("detail") or started.get("error") or "MIRC host refused the call"),
            })
            return
        call_id = self.state.next_call_id()
        with self.state.lock:
            self.state.calls[call_id] = {"channel": channel, "engine": engine}
        ws.send_json({
            "type": "ready",
            "callId": call_id,
            "channel": channel,
            "engine": engine,
            "sttProvider": active_stt_provider(self.state.stt_config),
        })
        try:
            while True:
                kind, payload = ws.recv_message()
                if kind == "close":
                    break
                if kind == "binary":
                    self._on_audio_chunk(ws, call_id, payload)
                else:
                    if self._on_control(ws, call_id, payload):
                        break
        finally:
            with self.state.lock:
                self.state.calls.pop(call_id, None)
            mirc_request(
                self.state.mirc_url,
                "/api/voice-call/call",
                {"action": "end", "channel": channel},
                timeout=10.0,
            )
            with contextlib.suppress(Exception):
                ws.send_json({"type": "ended", "callId": call_id})

    def _call_channel(self, call_id: str) -> str:
        with self.state.lock:
            return str((self.state.calls.get(call_id) or {}).get("channel") or "")

    def _on_audio_chunk(self, ws: WsConnection, call_id: str, payload: bytes) -> None:
        if not payload:
            return
        channel = self._call_channel(call_id)
        if not channel:
            return
        result = transcribe_chunk(payload, "audio/webm", self.state.stt_config)
        if result.get("success"):
            text = str(result.get("transcript") or "").strip()
            if text:
                ws.send_json({
                    "type": "transcript",
                    "callId": call_id,
                    "channel": channel,
                    "text": text,
                    "provider": str(result.get("provider") or ""),
                })
        else:
            logger.info("chunk rejected: %s", str(result.get("error") or "")[:160])

    def _on_control(self, ws: WsConnection, call_id: str, payload: bytes) -> bool:
        """Handle a control frame. True = hang up (break the loop)."""
        try:
            message = json.loads(payload.decode("utf-8"))
        except Exception:
            message = {}
        if not isinstance(message, dict):
            return False
        kind = str(message.get("type") or "")
        channel = self._call_channel(call_id)
        if kind == "tts":
            text = str(message.get("text") or "").strip()
            token = str(message.get("token") or "")
            if not text:
                ws.send_json({"type": "error", "message": "tts.text is required"})
                return False
            result = mirc_request(
                self.state.mirc_url, "/api/audio/speak", {"text": text}, timeout=90.0,
            )
            if result.get("ok"):
                ws.send_json({
                    "type": "audio",
                    "callId": call_id,
                    "token": token,
                    "mime": str(result.get("mime_type") or "audio/mpeg"),
                    "dataUrl": str(result.get("data_url") or ""),
                    "provider": str(result.get("provider") or ""),
                })
            else:
                ws.send_json({
                    "type": "error",
                    "token": token,
                    "message": str(result.get("error") or result.get("detail") or "TTS failed"),
                })
            return False
        if kind == "mute":
            muted = bool(message.get("muted", True))
            mirc_request(
                self.state.mirc_url,
                "/api/voice-call/call",
                {"action": "mute" if muted else "unmute", "channel": channel},
                timeout=10.0,
            )
            ws.send_json({"type": "muted", "callId": call_id, "muted": muted})
            return False
        if kind == "hangup":
            return True
        if kind == "ping":
            ws.send_json({"type": "pong", "callId": call_id})
            return False
        ws.send_json({"type": "error", "message": f"unknown control {kind!r}"})
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_voice_call_section() -> Dict[str, Any]:
    """Read voice_call config from the active Mercury home (never raises)."""
    try:
        from mercury_cli.config import load_config

        loaded = load_config() or {}
        section = loaded.get("voice_call")
        return dict(section) if isinstance(section, dict) else {}
    except Exception:
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Voice-call STT sidecar + signaling relay for the mLounge host.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Listen address (LAN IP for remote browsers)")
    parser.add_argument("--port", type=int, default=8765, help="Listen port")
    parser.add_argument("--mirc-url", default="", help="MIRC host base URL (or voice_call.mirc_host_url)")
    parser.add_argument("--provider", default="", help="STT provider overlay (qwen3-asr | parakeet | openai)")
    parser.add_argument("--model", default="", help="STT model overlay")
    parser.add_argument("--language", default="", help="STT language overlay")
    parser.add_argument("--endpoint", default="", help="STT endpoint overlay")
    parser.add_argument("--token", default="", help="Shared bearer token (or VOICE_CALL_SIDECAR_TOKEN)")
    parser.add_argument("--home", default="", help="Mercury home override (sets MERCURY_HOME/HERMES_HOME)")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.home:
        os.environ["MERCURY_HOME"] = args.home
        os.environ["HERMES_HOME"] = args.home
    section = load_voice_call_section()
    mirc_url = (args.mirc_url or str(section.get("mirc_host_url") or "")).strip().rstrip("/")
    if not mirc_url:
        print(
            "error: MIRC host URL is required (--mirc-url or voice_call.mirc_host_url). "
            "No localhost assumed.",
            file=sys.stderr,
        )
        return 2
    token = (args.token or os.environ.get("VOICE_CALL_SIDECAR_TOKEN") or "").strip()
    overlays = {
        "provider": args.provider,
        "model": args.model,
        "language": args.language,
        "endpoint": args.endpoint,
    }
    try:
        stt_config = load_sidecar_stt_config(overlays)
    except Exception as exc:
        print(f"error: cannot load STT config: {exc}", file=sys.stderr)
        return 2
    provider = active_stt_provider(stt_config)
    state = SidecarState(mirc_url=mirc_url, stt_config=stt_config, token=token)
    SidecarHandler.state = state
    server = ThreadingHTTPServer((args.host, args.port), SidecarHandler)
    server.daemon_threads = True
    print(f"voice-call sidecar: http://{args.host}:{args.port}")
    print(f"  MIRC host : {mirc_url}")
    print(f"  STT       : {provider} (browser-sourced audio only, no host mic)")
    print("  Browser WS: ws://<this-host>:<port>/call (explicit sidecar URL, no localhost default)")
    if not token:
        print("  warning: no --token set — any host that can reach this port can transcribe.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
