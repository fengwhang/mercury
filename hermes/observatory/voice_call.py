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
    """Return the live room's engine and registered name (never raises).

    Reads the room manager or its durable tree in one lookup. Missing,
    expired and unidentifiable rooms return unknown with an empty name.
    """
    unknown = {"engine": "unknown", "name": ""}
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
                row = db.execute(
                    "SELECT engine, name FROM nodes WHERE lower(room_id) = lower(?) "
                    "AND status = 'live' ORDER BY created_epoch DESC LIMIT 1",
                    (channel or "",),
                ).fetchone()
            return {"engine": str(row[0]), "name": str(row[1] or "")} if row else unknown
        _route, row = manager.inbound_route(channel or "")
        if row is None:
            return unknown
        engine = str(row.get("engine") or "").strip().lower()
        if engine not in {"hermes", "omp"}:
            return unknown
        return {"engine": engine, "name": str(row.get("name") or "")}
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
