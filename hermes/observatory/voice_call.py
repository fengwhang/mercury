"""First-class voice calls for Hermes engines, three-tier (no localhost).

Call flow (see docs/voice-call.md):

  browser (mic + speakers)
      │ mic audio up / reply audio down — one session socket per call
      ▼
  mLounge host — STT sidecar + signaling relay (observatory/voice_call_stt.py)
      │ transcripts up / TTS audio down (HTTP to the MIRC host)
      ▼
  MIRC host — Hermes conversation loop (unchanged gateway machinery) +
  TTS synthesis via the tts.provider from `mercury setup`
  (`/api/voice-call/*` registry/status here, audio via `/api/audio/speak`)

No tier uses a local-only audio device: the browser owns the microphone
(getUserMedia) and the speakers (HTMLAudio); both hosts move bytes over
the network. Same-machine is a degenerate case, never an assumption:
every hop reads explicit host URLs from the ``voice_call`` config
section — this module never falls back to localhost.

Hermes-only scope: ``check_engine_allowed`` / ``resolve_channel_agent``
allow only confirmed live Hermes rooms, using the durable tree when the
dashboard and gateway run in separate processes. OMP and unknown rooms are refused.
Nothing here imports omp machinery.
"""

from __future__ import annotations

import threading
import sqlite3
from typing import Any, Dict, List, Optional

#: Engines permitted to take voice calls. OMP rooms are refused loudly.
HERMES_VOICE_CALL_ENGINES = frozenset({"hermes"})

OMP_VOICE_CALL_REFUSAL = (
    "Voice calls are Hermes-engine only: this room is served by the OMP "
    "engine. Open (or spawn) a Hermes agent room to call."
)

#: Transcripts arrive as ordinary channel text; the marker exists so logs
#: and future auto-TTS hooks can tell call speech from typed chat.
VOICE_CALL_SOURCE = "voice-call"


class VoiceCallConfigError(RuntimeError):
    """A voice-call host URL is missing."""


class VoiceCallEngineError(RuntimeError):
    """A voice-call operation targeted a non-Hermes engine."""


# ---------------------------------------------------------------------------
# Engine scope
# ---------------------------------------------------------------------------


def resolve_channel_agent(channel: str) -> Dict[str, str]:
    """Return a live channel or query nick's registered identity.

    Channels match room_id; queries match the actual IRC nick in mxid,
    never a display name or room slug. Ambiguous/expired nicks fail closed.
    """
    unknown = {"engine": "unknown", "name": "", "room_id": ""}
    target = (channel or "").strip()
    is_channel = target.startswith(("#", "&"))
    try:
        from observatory.rooms import get_room_manager

        manager = get_room_manager()
        if manager is None:
            # The dashboard normally runs separately from the gateway.
            # Read its durable tree without creating or migrating the DB.
            from observatory.state import default_state_db_path

            path = default_state_db_path()
            if not path.is_file():
                return unknown
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                field = "room_id" if is_channel else "mxid"
                rows = db.execute(
                    f"SELECT engine, name, room_id, extra_json FROM nodes WHERE lower({field}) = lower(?) "
                    "AND status = 'live' ORDER BY created_epoch DESC LIMIT ?",
                    (target, 1 if is_channel else 2),
                ).fetchall()
            if not rows or (not is_channel and len(rows) != 1):
                return unknown
            import json
            row = {"engine": rows[0][0], "name": rows[0][1], "room_id": rows[0][2],
                   "extra": json.loads(rows[0][3] or "{}")}
        elif is_channel:
            _route, row = manager.inbound_route(target)
        else:
            want = target.lower()
            matches = [entry for entry in manager.live_rows()
                       if str(entry.get("mxid") or "").lower() == want]
            row = matches[0] if len(matches) == 1 else None
        if row is None:
            return unknown
        engine = str(row.get("engine") or "").strip().lower()
        if engine not in {"hermes", "omp"}:
            return unknown
        return {"engine": engine, "name": str(row.get("name") or ""),
                "room_id": str(row.get("room_id") or ""),
                "profile": str((row.get("extra") or {}).get("profile") or "default")}
    except Exception:
        return unknown


def check_engine_allowed(engine: str) -> tuple[bool, str]:
    """True + "" when *engine* may take voice calls, else False + reason."""
    key = (engine or "").strip().lower()
    if key in HERMES_VOICE_CALL_ENGINES:
        return True, ""
    if key == "omp":
        return False, OMP_VOICE_CALL_REFUSAL
    return False, "Cannot identify this room's engine. Start a call in a live Hermes agent room."


# ---------------------------------------------------------------------------
# Config (explicit hosts, no localhost assumption)
# ---------------------------------------------------------------------------


def voice_call_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the ``voice_call`` section merged over built-in defaults."""
    section: Dict[str, Any] = {}
    if config is None:
        try:
            from mercury_cli.config import load_config

            loaded = load_config() or {}
            raw = loaded.get("voice_call")
            if isinstance(raw, dict):
                section = dict(raw)
        except Exception:
            section = {}
    elif isinstance(config.get("voice_call"), dict):
        section = dict(config["voice_call"])
    defaults = {
        "enabled": True,
        "mirc_host_url": "",
        "mlounge_host_url": "",
        "stt_sidecar_url": "",
        "language": "en",
    }
    defaults.update(section)
    return defaults


def require_voice_call_hosts(
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Return the three split-host URLs, or raise naming what is missing."""
    section = voice_call_config(config)
    urls = {
        "mirc_host_url": str(section.get("mirc_host_url") or "").strip().rstrip("/"),
        "mlounge_host_url": str(section.get("mlounge_host_url") or "").strip().rstrip("/"),
        "stt_sidecar_url": str(section.get("stt_sidecar_url") or "").strip().rstrip("/"),
    }
    missing = [key for key, value in urls.items() if not value]
    if missing:
        raise VoiceCallConfigError(
            "Voice-call host URLs are not configured "
            f"(missing: {', '.join(missing)}). Set voice_call.{', voice_call.'.join(missing)} "
            "in config.yaml — e.g. via `mercury setup stt` — using explicit "
            "host URLs; localhost is never assumed."
        )
    return urls


# ---------------------------------------------------------------------------
# Call registry (active-call tracking for status UX)
# ---------------------------------------------------------------------------


class VoiceCallStore:
    """Thread-safe in-memory registry of active voice calls by channel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: Dict[str, Dict[str, Any]] = {}

    def start(
        self, channel: str, *, engine: str = "hermes", call_id: str = "",
    ) -> Dict[str, Any]:
        """Open or rejoin one socket-owned call on a channel."""
        allowed, reason = check_engine_allowed(engine)
        if not allowed:
            raise VoiceCallEngineError(reason)
        key = (channel or "").strip()
        if not key:
            raise ValueError("channel is required")
        with self._lock:
            record = self._calls.get(key)
            if record is None:
                record = {"channel": key, "engine": engine, "sessions": {}}
                self._calls[key] = record
            record["sessions"].setdefault(call_id, False)
            return self._record_status(record)

    def end(self, channel: str, *, call_id: Optional[str] = None) -> bool:
        """Close one socket owner, or all owners for a channel-wide End."""
        key = (channel or "").strip()
        with self._lock:
            if call_id is None:
                return self._calls.pop(key, None) is not None
            record = self._calls.get(key)
            if record is None or call_id not in record["sessions"]:
                return False
            del record["sessions"][call_id]
            if not record["sessions"]:
                del self._calls[key]
            return True

    def set_muted(
        self, channel: str, muted: bool, *, call_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update one socket owner, or every owner on the channel."""
        key = (channel or "").strip()
        with self._lock:
            record = self._calls.get(key)
            if record is None or (call_id is not None and call_id not in record["sessions"]):
                return None
            for owner in record["sessions"]:
                if call_id is None or owner == call_id:
                    record["sessions"][owner] = bool(muted)
            return self._record_status(record)

    @staticmethod
    def _record_status(record: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "channel": record["channel"], "engine": record["engine"],
            "muted": all(record["sessions"].values()),
        }

    def status(self, channel: str) -> Dict[str, Any]:
        """Call state for *channel* (active False when idle)."""
        key = (channel or "").strip()
        with self._lock:
            record = self._calls.get(key)
            if record is None:
                return {"channel": key, "active": False, "muted": False, "engine": "hermes"}
            return {"active": True, **self._record_status(record)}

    def active_channels(self) -> List[str]:
        """Every channel with an open call."""
        with self._lock:
            return sorted(self._calls)


_DEFAULT_STORE = VoiceCallStore()


def default_store() -> VoiceCallStore:
    """Process-wide call registry (gateway process)."""
    return _DEFAULT_STORE


# ---------------------------------------------------------------------------
# Transcript data path (TTS audio flows over /api/audio/speak)
# ---------------------------------------------------------------------------


def transcript_envelope(channel: str, text: str) -> Dict[str, Any]:
    """Wire shape for one STT transcript headed to the agent.

    The frontend delivers ``text`` as a plain channel message (existing
    ``socket.emit("input", {target, text})`` path → normal MIRC inbound →
    Hermes turn). The envelope documents the hop for logs and tests; the
    host needs no new message kind.
    """
    return {
        "channel": (channel or "").strip(),
        "text": text or "",
        "source": VOICE_CALL_SOURCE,
        "engine": "hermes",
    }


def create_voice_service_app():
    """Expose only existing canonical voice handlers behind service auth."""
    import hmac
    import os
    from fastapi import FastAPI
    from starlette.responses import JSONResponse
    from mercury_cli import web_server
    from mercury_cli.config import get_env_value
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def service_auth(request, call_next):
        expected = get_env_value("VOICE_CALL_MIRC_TOKEN") or ""
        presented = request.headers.get("authorization", "")
        if not expected or request.headers.get("origin") or not hmac.compare_digest(
            presented.encode(), f"Bearer {expected}".encode()
        ):
            return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        profile = request.query_params.get("profile") or "default"
        if profile != (os.environ.get("HERMES_PROFILE") or "default"):
            return JSONResponse(status_code=403, content={"detail": "Voice service profile not authorized"})
        if not voice_call_config().get("enabled", True):
            return JSONResponse(status_code=503, content={"detail": "Voice calls disabled in Mercury Setup"})
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = -1
        if size < 0 or size > 65536 or request.headers.get("transfer-encoding"):
            return JSONResponse(status_code=413, content={"detail": "Voice request exceeds limits"})
        if request.url.path == "/api/audio/speak":
            from tools.tool_backend_helpers import read_selection
            with web_server._config_profile_scope(profile):
                if read_selection("tts") is None:
                    return JSONResponse(status_code=503, content={"detail": "Configure TTS in Mercury Setup on the MIRC host; no cloud fallback"})
        return await call_next(request)

    app.add_api_route("/api/voice-call/status", web_server.voice_call_status, methods=["GET"])
    app.add_api_route("/api/voice-call/call", web_server.voice_call_action, methods=["POST"])
    app.add_api_route("/api/audio/speak", web_server.speak_text, methods=["POST"])
    return app


def configure_mirc_voice_unit(installation) -> str:
    """Generate the MIRC host unit for the existing authenticated voice API."""
    from pathlib import Path
    import secrets
    import sys
    from urllib.parse import urlsplit
    from mercury_cli.config import get_config_path, get_env_path, load_env, save_env_value
    from mercury_constants import get_hermes_home
    cfg = voice_call_config()
    endpoint = urlsplit(str(cfg.get("mirc_host_url") or ""))
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/"):
        raise VoiceCallConfigError("Configure explicit voice_call.mirc_host_url in Mercury Setup")
    install = Path(installation).resolve()
    runtime = get_hermes_home().resolve()
    if runtime != install and not runtime.is_relative_to(install / "hermes" / "profiles"):
        raise VoiceCallConfigError("MIRC voice profile belongs to a different installation")
    if not load_env().get("VOICE_CALL_MIRC_TOKEN"):
        save_env_value("VOICE_CALL_MIRC_TOKEN", secrets.token_urlsafe(32))
    profile = runtime.name if runtime != install else "default"
    directory = install / "observatory" / "voice"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = "mercury-nightly-mirc-voice.service" if "nightly" in install.name else "mercury-mirc-voice.service"
    unit = directory / name
    unit.write_text("\n".join((
        "[Unit]", "Description=Mercury authenticated Hermes voice API",
        "After=network-online.target", "[Service]", "Type=simple",
        f"WorkingDirectory={Path(__file__).resolve().parents[1]}",
        f'Environment="MERCURY_HOME={install}"',
        f'Environment="HERMES_HOME={runtime}"',
        f'Environment="HERMES_PROFILE={profile}"',
        f'Environment="MERCURY_CONFIG={get_config_path()}"',
        f'EnvironmentFile={get_env_path()}',
        f'ExecStart={sys.executable} -m observatory.voice_call --host {endpoint.hostname} --port {endpoint.port or (443 if endpoint.scheme == "https" else 80)}',
        "Restart=on-failure", "KillMode=control-group", "TimeoutStopSec=10",
        "[Install]", "WantedBy=default.target", "",
    )), encoding="utf-8")
    return str(unit)


def main(argv=None) -> int:
    """Setup-managed MIRC voice API; no dashboard, mic, speaker or agent loop."""
    import argparse
    import importlib.util
    import os
    import sys
    parser = argparse.ArgumentParser(description="Configured Hermes voice service")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if any(importlib.util.find_spec(name) is None for name in ("fastapi", "uvicorn")):
        print("Voice API dependencies unavailable; provision cached FastAPI/uvicorn on the MIRC host via Setup/doctor. No automatic install.", file=sys.stderr)
        return 2
    from mercury_cli.config import get_env_value
    if not get_env_value("VOICE_CALL_MIRC_TOKEN"):
        print("Voice API authentication missing; rerun Mercury Setup on the MIRC host.", file=sys.stderr)
        return 2
    import uvicorn
    cfg = voice_call_config()
    tls = {}
    from urllib.parse import urlsplit
    if urlsplit(str(cfg.get("mirc_host_url") or "")).scheme == "https":
        if not cfg.get("tls_cert") or not cfg.get("tls_key"):
            print("HTTPS MIRC voice service needs configured voice_call.tls_cert/tls_key on this host.", file=sys.stderr)
            return 2
        tls = {"ssl_certfile": cfg["tls_cert"], "ssl_keyfile": cfg["tls_key"]}
    uvicorn.run(create_voice_service_app(), host=args.host, port=args.port,
                proxy_headers=False, log_level="warning", **tls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
