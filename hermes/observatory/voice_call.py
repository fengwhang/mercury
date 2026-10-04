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

Hermes-only scope: ``check_engine_allowed`` / ``resolve_channel_engine``
deny OMP rooms positively (spawn-omp route, omp-engine child rows) and
fail open otherwise (gateway sessions and fresh channels are Hermes).
Nothing here imports omp machinery.
"""

from __future__ import annotations

import threading
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


def resolve_channel_engine(channel: str) -> str:
    """Return ``"hermes"`` or ``"omp"`` for a MIRC channel (never raises).

    Reads the live room manager when one is registered and denies only on
    positive OMP evidence (spawn-omp route, omp-engine child row). Gateway
    sessions, Hermes rooms, and unknown channels resolve to Hermes — the
    gateway turn runner is Hermes machinery.
    """
    try:
        from observatory.rooms import get_room_manager

        manager = get_room_manager()
        if manager is None:
            return "hermes"
        route, row = manager.inbound_route(channel or "")
        if route == "spawn-omp":
            return "omp"
        if route == "child":
            try:
                if str((row or {}).get("engine") or "") == "omp":
                    return "omp"
            except Exception:
                pass
            return "hermes"
        return "hermes"
    except Exception:
        return "hermes"


def check_engine_allowed(engine: str) -> tuple[bool, str]:
    """True + "" when *engine* may take voice calls, else False + reason."""
    key = (engine or "").strip().lower()
    if key in HERMES_VOICE_CALL_ENGINES:
        return True, ""
    if key == "omp":
        return False, OMP_VOICE_CALL_REFUSAL
    # Unknown engine labels fail open with a note — the room routes that
    # matter (spawn-omp) resolve positively above.
    return True, ""


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

    def start(self, channel: str, *, engine: str = "hermes") -> Dict[str, Any]:
        """Open (or rejoin) a call. Raises VoiceCallEngineError for OMP."""
        allowed, reason = check_engine_allowed(engine)
        if not allowed:
            raise VoiceCallEngineError(reason)
        key = (channel or "").strip()
        if not key:
            raise ValueError("channel is required")
        with self._lock:
            record = self._calls.get(key)
            if record is None:
                record = {"channel": key, "engine": engine, "muted": False}
                self._calls[key] = record
            else:
                record["engine"] = engine
            return dict(record)

    def end(self, channel: str) -> bool:
        """Close a call. True when one was active."""
        key = (channel or "").strip()
        with self._lock:
            return self._calls.pop(key, None) is not None

    def set_muted(self, channel: str, muted: bool) -> Optional[Dict[str, Any]]:
        """Mute/unmute a call. None when no call is active."""
        key = (channel or "").strip()
        with self._lock:
            record = self._calls.get(key)
            if record is None:
                return None
            record["muted"] = bool(muted)
            return dict(record)

    def status(self, channel: str) -> Dict[str, Any]:
        """Call state for *channel* (active False when idle)."""
        key = (channel or "").strip()
        with self._lock:
            record = self._calls.get(key)
            if record is None:
                return {"channel": key, "active": False, "muted": False, "engine": "hermes"}
            return {"channel": key, "active": True, **{k: v for k, v in record.items() if k != "channel"}}

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


