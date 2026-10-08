#!/usr/bin/env python3
"""Voice-call STT sidecar + signaling relay for the mLounge host.

Three-tier audio path (see docs/voice-call.md)::

  browser (getUserMedia mic / HTMLAudio speakers)
      │ authenticated mLounge session -> private /call service socket
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
  or ``voice_call.mirc_host_url``); mLounge reaches the configured private
  STT peer using a service header. The browser never supplies peer URLs
  or service/provider credentials.
- Hermes-only: the opening ``hello`` resolves the channel engine through
  the MIRC host and refuses OMP rooms before any audio flows.

Setup provisions this listener from saved host/profile configuration.
Service credentials are loaded from the selected host's .env, never argv.
The transport uses stdlib HTTP/WebSocket framing and canonical Hermes STT.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import queue
import shutil
import socket
import struct
import sys
import tempfile
import threading
import uuid
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

logger = logging.getLogger("voice_call_stt")

REPO_HERMES = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(REPO_HERMES, "tools")):
    sys.path.insert(0, os.path.dirname(REPO_HERMES))

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_MAX_MESSAGE_BYTES = 8 * 1024 * 1024

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
    if first & 0x70 or not masked:
        raise ValueError("client websocket frames must be masked without reserved bits")
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _read_exact(rfile, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exact(rfile, 8))
    if length > WS_MAX_MESSAGE_BYTES:
        raise ValueError("websocket frame too large")
    if opcode >= 0x8 and (not fin or length > 125):
        raise ValueError("invalid websocket control frame")
    mask = _read_exact(rfile, 4) if masked else b""
    payload = _read_exact(rfile, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WsConnection:
    """Blocking server-side connection over an hijacked HTTP socket."""

    def __init__(self, conn: socket.socket, rfile=None) -> None:
        self.conn = conn
        self.rfile = rfile if rfile is not None else conn.makefile("rb")
        self.lock = threading.Lock()
        self.closed = False
        self.messages: Optional[queue.Queue[tuple[str, bytes]]] = None

    def watch_startup(self) -> None:
        """One reader owns transport EOF even while status/start HTTP is blocked."""
        messages: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=12)
        self.messages = messages

        def read():
            try:
                while not self.closed:
                    message = self._read_message()
                    if message[0] == "close":
                        break
                    messages.put_nowait(message)
            except (OSError, ValueError, queue.Full):
                pass
            finally:
                self.close()
                # A full queue is discarded only after the transport is closed.
                while not messages.empty():
                    with contextlib.suppress(queue.Empty):
                        messages.get_nowait()
                messages.put_nowait(("close", b""))

        threading.Thread(target=read, daemon=True).start()

    def send_json(self, message: Dict[str, Any], *, guard=None, timeout=None) -> None:
        raw = json.dumps(message).encode("utf-8")
        self._send_frame(raw, 0x1, guard=guard, timeout=timeout)

    def _send_frame(self, payload: bytes, opcode: int, *, guard=None, timeout=None) -> None:
        locked = self.lock.acquire() if timeout is None else self.lock.acquire(timeout=timeout)
        if not locked:
            self.close(1011, "Voice socket writer blocked")
            raise ConnectionError("Voice socket writer blocked; call ended")
        failure = None
        try:
            if self.closed or (guard is not None and not guard()):
                return
            previous_timeout = self.conn.gettimeout() if timeout is not None else None
            try:
                if timeout is not None:
                    self.conn.settimeout(timeout)
                self.conn.sendall(ws_encode_frame(payload, opcode))
            except OSError as exc:
                self.closed = True
                failure = exc
            finally:
                if timeout is not None and not self.closed:
                    self.conn.settimeout(previous_timeout)
        finally:
            self.lock.release()
        if self.closed:
            self.close()
        if failure is not None and timeout is not None:
            raise ConnectionError("Voice control response failed; call ended") from failure

    def recv_message(self) -> tuple[str, bytes]:
        if self.messages is not None:
            return self.messages.get()
        return self._read_message()

    def _read_message(self) -> tuple[str, bytes]:
        """Next complete message as (kind, payload): text|binary|close."""
        fragments: list[bytes] = []
        text_mode: Optional[bool] = None
        message_bytes = 0
        while True:
            fin, opcode, payload = ws_decode_frame(self.rfile)
            if opcode == 0x8:  # close
                return "close", payload
            if opcode == 0x9:  # ping
                self._send_frame(payload, 0xA, timeout=0.1)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                if text_mode is not None:
                    raise ValueError("new websocket message before final continuation")
                text_mode = opcode == 0x1
            elif opcode != 0x0:
                raise ValueError(f"unsupported websocket opcode {opcode}")
            elif text_mode is None:
                raise ValueError("websocket continuation without message")
            message_bytes += len(payload)
            if message_bytes > WS_MAX_MESSAGE_BYTES:
                raise ValueError("websocket message too large")
            fragments.append(payload)
            if fin:
                kind = "text" if text_mode else "binary"
                return kind, b"".join(fragments)

    def close(
        self, code: int = 1000, reason: str = "", *, message: Optional[Dict[str, Any]] = None,
    ) -> None:
        # Give a finishing writer a bounded chance to release the lock so a
        # normal hangup delivers its final notice and RFC close frame. A
        # backpressured writer still gets interrupted by shutdown below.
        locked = self.lock.acquire(timeout=0.1)
        was_closed = self.closed
        self.closed = True
        try:
            if locked and not was_closed:
                try:
                    self.conn.settimeout(0.1)
                    frame = ws_encode_frame(struct.pack("!H", code) + reason.encode("utf-8"), 0x8)
                    if message is not None:
                        frame = ws_encode_frame(json.dumps(message).encode("utf-8")) + frame
                    self.conn.sendall(frame)
                except OSError:
                    pass
        finally:
            with contextlib.suppress(OSError):
                self.conn.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                self.conn.close()
            if locked:
                self.lock.release()


# ---------------------------------------------------------------------------
# MIRC host proxy (urllib, stdlib)
# ---------------------------------------------------------------------------


def mirc_request(
    mirc_url: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    timeout: float = 60.0,
    token: str = "",
) -> Dict[str, Any]:
    """POST/GET JSON against the MIRC host. Never raises: errors → dict."""
    import re
    import urllib.error

    url = mirc_url.rstrip("/") + path
    try:
        data = None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
        parsed = json.loads(body) if body.strip() else {}
        return parsed if isinstance(parsed, dict) else {"ok": False, "error": "bad MIRC response"}
    except urllib.error.HTTPError as exc:
        # HTTP failures are responses. Only bounded, canonical public messages
        # may cross this service boundary; arbitrary provider bodies are private.
        result = {"ok": False, "status": exc.code, "error": "request failed"}
        try:
            with exc:
                raw = exc.read(4097)
            if exc.code in (401, 403) or len(raw) > 4096:
                return result
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict):
                return result
            message = parsed.get("detail") or parsed.get("error")
            if not isinstance(message, str) or not 0 < len(message) <= 512:
                return result
            public = {
                "Voice calls disabled in Mercury Setup",
                "Voice request exceeds limits",
                "channel is required",
                "action must be start|end|mute|unmute",
                "no active call on channel",
            }
            configuration = re.fullmatch(
                r"Configure (?:STT|TTS) in Mercury Setup on the (?:MIRC|mLounge) host"
                r"(?:; no cloud fallback)?",
                message,
            )
            secrets = (token, os.environ.get("VOICE_CALL_MIRC_TOKEN", ""),
                       os.environ.get("VOICE_CALL_SIDECAR_TOKEN", ""))
            if (message in public or configuration) and not any(
                secret and secret in message for secret in secrets
            ):
                result["error"] = message
        except (OSError, ValueError, UnicodeError):
            pass
        return result
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "error": "MIRC host unreachable"}
    except Exception:
        return {"ok": False, "error": "request failed"}


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
    if not overlays.get("provider"):
        from tools.tool_backend_helpers import read_selection
        if read_selection("stt") is None:
            stt_config["provider"] = "none"
    provider = (overlays.get("provider") or "").strip().lower()
    if provider:
        stt_config["provider"] = provider
    elif any(overlays.get(key) for key in ("model", "language", "endpoint")):
        provider = active_stt_provider(stt_config)
    if provider:
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
        from tools import transcription_tools as tt
        if not tt.is_stt_enabled(stt_config) or not stt_config.get("provider"):
            return "none"
        if stt_config.get("provider") == "local" and not tt._HAS_FASTER_WHISPER and not tt._has_local_command():
            return "none"
        return str(tt._get_provider(stt_config))
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
    if not stt_config.get("enabled", True):
        return {"success": False, "transcript": "", "error": "STT is disabled; enable it in Mercury Setup on the mLounge host."}
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
        provider = active_stt_provider(stt_config)
        if provider == "none" or provider.startswith("unavailable"):
            return {"success": False, "transcript": "", "error": "Configured STT unavailable; run Mercury Setup/doctor on the mLounge host. No automatic install or cloud fallback."}
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
        mirc_token: str = "",
        profile: str = "default",
        reload_config: bool = False,
    ) -> None:
        self.mirc_url = mirc_url.rstrip("/")
        self.stt_config = stt_config
        self.token = token
        self.mirc_token = mirc_token
        self.profile = profile
        self.reload_config = reload_config
        self.calls: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def check_token(self, handler: BaseHTTPRequestHandler) -> bool:
        # This listener is a private service peer, never browser-authenticated.
        if not self.token or handler.headers.get("Origin") or urllib.parse.urlparse(handler.path).query:
            return False
        presented = (handler.headers.get("X-Voice-Call-Token") or "").strip()
        return hmac.compare_digest(presented.encode(), self.token.encode())

    def request(self, path: str, payload=None, *, timeout=60.0):
        path += ("&" if "?" in path else "?") + "profile=" + urllib.parse.quote(self.profile, safe="")
        return mirc_request(self.mirc_url, path, payload, timeout=timeout, token=self.mirc_token)

    def next_call_id(self) -> str:
        return f"call-{uuid.uuid4().hex}"


class SidecarHandler(BaseHTTPRequestHandler):
    state: SidecarState
    server_version = "VoiceCallSTT/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)


    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802 — handler naming convention
        self.send_response(204)
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
            result = self.state.request(
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
        result = self.state.request(
                "/api/audio/speak", {"text": text}, timeout=90.0,
        )
        self._send_json(result, 200 if result.get("ok") else 502)

    def _handle_action_proxy(self, body: Dict[str, Any]) -> None:
        action = str(body.get("action") or "").strip().lower()
        channel = str(body.get("channel") or "").strip()
        if action not in ("start", "end", "mute", "unmute") or not channel:
            self._send_json({"ok": False, "error": "action must be start|end|mute|unmute with channel"}, 400)
            return
        result = self.state.request(
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
            self.send_header("Content-Length", "0")
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
        ws = WsConnection(conn, self.rfile)
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
        mime = str(hello.get("mime") or "audio/webm").split(";")[0].lower()
        if mime not in CHUNK_SUFFIX_BY_MIME:
            ws.send_json({"type": "error", "message": "unsupported audio container"})
            return
        if not channel:
            ws.send_json({"type": "error", "message": "hello.channel is required"})
            return
        if isinstance(ws, WsConnection):
            ws.watch_startup()
        status = self.state.request(
                f"/api/voice-call/status?channel={urllib.parse.quote(channel)}",
            None,
            timeout=10.0,
        )
        engine = str(status.get("engine") or "unknown")
        if getattr(ws, "closed", False):
            return
        if not status.get("ok") or status.get("allowed") is not True:
            ws.send_json({
                "type": "refused",
                "reason": str(status.get("reason") or status.get("error") or "call not allowed"),
            })
            return
        # Resolve the engine first: OMP refusal must not touch voice providers.
        if engine != "hermes":
            ws.send_json({"type": "refused", "reason": "Voice calls require a confirmed Hermes engine"})
            return
        call_config = load_sidecar_stt_config() if self.state.reload_config else self.state.stt_config
        if self.state.reload_config and active_stt_provider(call_config) == "none":
            ws.send_json({"type": "refused", "reason": "Configured STT unavailable; rerun Mercury Setup/doctor on the mLounge host"})
            return
        call_id = self.state.next_call_id()
        if getattr(ws, "closed", False):
            return
        started = self.state.request(
                "/api/voice-call/call",
            {"action": "start", "channel": channel, "engine": engine, "call_id": call_id},
            timeout=10.0,
        )
        if getattr(ws, "closed", False):
            # Start was already in flight: its outcome may be unknown. Reconcile
            # exactly the UUID we submitted, never replay start or end a room.
            self.state.request(
                "/api/voice-call/call",
                {"action": "end", "channel": channel, "call_id": call_id},
                timeout=10.0,
            )
            return
        if not started.get("ok"):
            try:
                ws.send_json({
                    "type": "refused",
                    "reason": str(started.get("detail") or started.get("error") or "MIRC host refused the call"),
                })
            finally:
                # The request can commit remotely before its reply is lost.
                # UUID ownership makes uncertain-start cleanup idempotent
                # without ending any other browser's call on this target.
                ws.close()
                self.state.request(
                    "/api/voice-call/call",
                    {"action": "end", "channel": channel, "call_id": call_id},
                    timeout=10.0,
                )
            return
        with self.state.lock:
            self.state.calls[call_id] = {
                "channel": channel, "engine": engine, "mime": mime,
                "muted": False, "audio_epoch": 0,
                "stt_config": call_config,
            }
        # STT and synthesis can each block on a provider. Keep the socket
        # reader free for microphone traffic, ping, mute, and hangup, with
        # bounded queues and one worker per stream to preserve reply order.
        done = threading.Event()
        audio_jobs = queue.Queue(maxsize=4)
        speech_jobs = queue.Queue(maxsize=8)
        registry_jobs = queue.Queue(maxsize=8)

        def worker(jobs, operation):
            while not done.is_set():
                try:
                    data = jobs.get(timeout=0.2)
                except queue.Empty:
                    continue
                if not done.is_set():
                    try:
                        operation(ws, call_id, data)
                    except Exception:
                        logger.exception("voice worker failed")
                        done.set()
                        with contextlib.suppress(Exception):
                            ws.send_json({"type": "error", "message": "Voice worker failed; call ended."}, timeout=0.1)
                        ws.close()
                        return


        def enqueue(jobs, data):
            try:
                jobs.put_nowait(data)
            except queue.Full:
                ws.send_json({"type": "error", "message": "Voice provider is too slow; audio queue is full."}, timeout=0.1)

        try:
            for jobs, operation in (
                (audio_jobs, self._on_audio_chunk),
                (speech_jobs, self._on_control),
                (registry_jobs, self._sync_muted),
            ):
                threading.Thread(target=worker, args=(jobs, operation), daemon=True).start()
            ws.send_json({
                "type": "ready",
                "callId": call_id,
                "channel": channel,
                "engine": engine,
                "agentName": str(status.get("agent_name") or ""),
                "agentRoom": str(status.get("agent_room") or ""),
                "sttProvider": active_stt_provider(call_config),
            })
            while True:
                kind, payload = ws.recv_message()
                if kind == "close":
                    break
                if kind == "binary":
                    with self.state.lock:
                        call = self.state.calls.get(call_id) or {}
                        if call.get("muted"):
                            continue
                        epoch = call.get("audio_epoch", 0)
                    enqueue(audio_jobs, (epoch, payload))
                else:
                    try:
                        control = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        control = None
                    if isinstance(control, dict) and control.get("type") == "tts":
                        enqueue(speech_jobs, payload)
                        continue
                    if self._on_control(ws, call_id, payload):
                        break
                    if isinstance(control, dict) and control.get("type") == "mute":
                        enqueue(registry_jobs, payload)
        finally:
            done.set()
            with self.state.lock:
                self.state.calls.pop(call_id, None)
            ws.close(message={"type": "ended", "callId": call_id})
            self.state.request(
                "/api/voice-call/call",
                {"action": "end", "channel": channel, "call_id": call_id},
                timeout=10.0,
            )

    def _call_channel(self, call_id: str) -> str:
        with self.state.lock:
            return str((self.state.calls.get(call_id) or {}).get("channel") or "")

    def _call_authorized(self, ws: WsConnection, call_id: str) -> bool:
        """Revalidate the selected host/profile's live node before provider effects."""
        channel = self._call_channel(call_id)
        if not channel or ws.closed:
            return False
        status = self.state.request(
            f"/api/voice-call/status?channel={urllib.parse.quote(channel)}",
            None, timeout=10.0)
        if status.get("ok") and status.get("allowed") is True and status.get("engine") == "hermes":
            return not ws.closed and self._call_channel(call_id) == channel
        ws.close(message={"type": "refused", "callId": call_id,
                          "reason": str(status.get("reason") or status.get("error") or "Call target expired or changed engine/profile")})
        return False

    def _audio_current(self, call_id: str, epoch: int) -> bool:
        with self.state.lock:
            call = self.state.calls.get(call_id)
            return bool(call and not call["muted"] and call["audio_epoch"] == epoch)

    def _on_audio_chunk(self, ws: WsConnection, call_id: str, job: tuple[int, bytes]) -> None:
        epoch, payload = job
        if not payload:
            return
        with self.state.lock:
            call = self.state.calls.get(call_id)
            if not call or call["muted"] or call["audio_epoch"] != epoch:
                return
            channel, mime = call["channel"], call["mime"]
        if not self._call_authorized(ws, call_id) or ws.closed or not self._audio_current(call_id, epoch):
            return
        result = transcribe_chunk(payload, mime, call.get("stt_config", self.state.stt_config))
        with self.state.lock:
            call = self.state.calls.get(call_id)
            if not call or call["muted"] or call["audio_epoch"] != epoch:
                return
        if result.get("success"):
            text = str(result.get("transcript") or "").strip()
            if text:
                ws.send_json({
                    "type": "transcript",
                    "callId": call_id,
                    "channel": channel,
                    "text": text,
                    "provider": str(result.get("provider") or ""),
                }, guard=lambda: self._audio_current(call_id, epoch))
        else:
            ws.send_json(
                {"type": "error", "message": str(result.get("error") or "transcription failed")},
                guard=lambda: self._audio_current(call_id, epoch),
            )

    def _sync_muted(self, ws: WsConnection, call_id: str, payload: bytes) -> None:
        channel = self._call_channel(call_id)
        if not channel:
            return
        muted = bool(json.loads(payload).get("muted", True))
        self.state.request(
            "/api/voice-call/call",
            {"action": "mute" if muted else "unmute", "channel": channel, "call_id": call_id},
            timeout=10.0,
        )

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
        if not channel or message.get("callId", call_id) != call_id:
            return True
        if kind == "tts":
            if not self._call_authorized(ws, call_id):
                return True
            text = str(message.get("text") or "").strip()
            token = str(message.get("token") or "")
            if not text or len(text) > 16000 or len(token) > 128:
                ws.send_json({"type": "error", "message": "tts.text is empty or exceeds call limits"})
                return False
            result = self.state.request(
                "/api/audio/speak", {"text": text}, timeout=90.0,
            )
            if not self._call_channel(call_id):
                return False
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
            with self.state.lock:
                call = self.state.calls.get(call_id)
                if not call:
                    return False
                call["muted"] = muted
                call["audio_epoch"] += 1
            ws.send_json({"type": "muted", "callId": call_id, "muted": muted}, timeout=0.1)
            return False
        if kind == "hangup":
            return True
        if kind == "ping":
            ws.send_json({"type": "pong", "callId": call_id}, timeout=0.1)
            return False
        ws.send_json({"type": "error", "message": f"unknown control {kind!r}"}, timeout=0.1)
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
    parser.add_argument("--mirc-token", default="", help="MIRC voice service secret (or VOICE_CALL_MIRC_TOKEN)")
    parser.add_argument("--home", default="", help="Mercury home override (including its config.yaml)")
    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if args.home:
        os.environ["MERCURY_HOME"] = args.home
        os.environ["HERMES_HOME"] = args.home
        os.environ["MERCURY_CONFIG"] = os.path.join(args.home, "config.yaml")
    section = load_voice_call_section()
    mirc_url = (args.mirc_url or str(section.get("mirc_host_url") or "")).strip().rstrip("/")
    if not mirc_url:
        print(
            "error: MIRC host URL is required (--mirc-url or voice_call.mirc_host_url). "
            "No localhost assumed.",
            file=sys.stderr,
        )
        return 2
    from mercury_cli.config import get_env_value
    token = (args.token or get_env_value("VOICE_CALL_SIDECAR_TOKEN") or "").strip()
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
    mirc_token = (args.mirc_token or get_env_value("VOICE_CALL_MIRC_TOKEN") or "").strip()
    if not token or not mirc_token:
        print("error: voice service authentication missing; rerun host Mercury Setup.", file=sys.stderr)
        return 2
    state = SidecarState(mirc_url=mirc_url, stt_config=stt_config, token=token, mirc_token=mirc_token,
                         profile=os.environ.get("HERMES_PROFILE") or "default",
                         reload_config=not any(overlays.values()))
    SidecarHandler.state = state
    tls_context = None
    if urllib.parse.urlsplit(str(section.get("stt_sidecar_url") or "")).scheme == "https":
        if not section.get("tls_cert") or not section.get("tls_key"):
            print("error: HTTPS STT service needs voice_call.tls_cert/tls_key on this host; configure them in host Setup.", file=sys.stderr)
            return 2
        import ssl
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(section["tls_cert"], section["tls_key"])
    server = ThreadingHTTPServer((args.host, args.port), SidecarHandler)
    if tls_context:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    print(f"voice-call sidecar: http://{args.host}:{args.port}")
    print(f"  MIRC host : {mirc_url}")
    print(f"  STT       : {provider} (browser-sourced audio only, no host mic)")
    print("  Transport : authenticated mLounge relay (no browser credentials)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
