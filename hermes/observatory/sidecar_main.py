"""M4c/M5c: the Observatory sidecar daemon — ONE asyncio process owning
ALL Matrix I/O (spec §2 component 2, D3).

Assembly, in boot order:

1. **Provision** (idempotent, offline-aware) via
   :mod:`observatory.provision` — binary, closed tuwunel.toml, appservice
   registration, owner account, homeserver systemd unit.
2. **Homeserver up**: health-probe ``/_matrix/client/versions``; start the
   systemd unit when idle, else (no systemd / CI / smoke) boot the binary
   as a child process we own and terminate on shutdown.
3. **State + gateway node**: ``$MERCURY_HOME/observatory/state.db``; the
   gateway agent node (depth 0, ``extra.kind="gateway"`` — the
   render_live/spawn.py convention) is ensured before anything renders.
4. **E2EE** (D4) when ``observatory.e2ee`` is true: one
   :class:`~observatory.e2ee.E2EEManager`; the renderer's executor is
   wrapped (``EncryptedIntentExecutor`` — coordinate via wrapper, never
   by editing renderer.py) so every chat room it creates is encrypted at
   creation. Flag off (default) ⇒ plain
   :class:`~observatory.renderer.IntentExecutor`. Flag on + crypto stack
   missing ⇒ hard failure with the remedy (O3: never fake crypto).
5. **D18 respawn BEFORE traffic**: adopt the gateway-thread boot's
   registry (``platform_hook.LAST_BOOT`` — ``try_boot_sidecar`` may have
   booted on the gateway side) or a fresh one, then run
   :func:`observatory.respawn.respawn_pass` (purge-journal replay +
   0-agent resume + room re-ensure). Restart is not death: shutdown never
   touches the registry's live handles — the next respawn re-adopts them.
6. **Renderer converge**: ensure virtual users, apply the §3 space plan.
7. **TransactionIntake**: aiohttp appservice endpoint on
   ``127.0.0.1:18090`` served inside THIS loop (AppRunner, not
   ``web.run_app``). Inbound pipeline per transaction: E2EE decrypt →
   directives-room delivery (§6) → control router (M4a) + approval
   bridge (M4b) → notices posted back into rooms.
8. **Discovery** (§7): :class:`~observatory.discovery.DiscoveryEngine`
   over ``$MERCURY_HOME/hermes/state.db`` (via
   ``platform_hook.build_discovery``) — NodeEvents map to state nodes
   and renderer lifecycle/death renders.
9. **Sibling subsystems** (M4a/M4b/M5 — landed in parallel, integrated
   here, never edited): ControlRouter, ApprovalBridge (+ expiry loop),
   DirectivesManager (membership sync), CronRooms (+ render_poll loop),
   ManualRunsWatcher (+ render_poll loop), OrchestratorRegistry omp
   feeds (one :class:`~observatory.omp_feed.OmpFeed` per live RPC child).
   Gateway-node InjectText delivers over the gateway control socket
   (``inject`` verb → one headless turn → reply renders in the room);
   spawned 0-agent child actions deliver against the daemon registry
   (hermes turns run on the child's own handle, omp prompts/steers go
   over its RPC transport, aborts interrupt) — see
   ``_execute_child_action``. Grandchild subagent actions fan out over
   the ancestor child's transport. Anything still without a transport
   stays logged in ``routing_log``.
10. **Graceful shutdown** (SIGINT/SIGTERM): stop loops + feeds +
    discovery, stop the intake, flush state, terminate an owned
    homeserver. The systemd unit never owns homeserver lifetime here
    (its own unit restarts it).

Entry points::

    python -m observatory.sidecar_main [--home MERCURY_HOME] [--once-smoke]

``--once-smoke`` runs the full boot against a THROWAWAY testhome
tuwunel (provision + boot + intake + shutdown) and prints
``MERCURY-M4C-OK`` on success — or ``MERCURY-M4C-E2EE-OK`` when E2EE is
enabled AND one message round-trips encrypted (impossible without the
compiled olm stack; see observatory/e2ee.py REMAINING WORK).

The gateway seam (``observatory.platform_hook.try_boot_sidecar`` — one
additive insertion in gateway/run.py, NOT edited by M4c) boots the
observatory on the gateway thread; this daemon ADOPTS that boot's
registry (``LAST_BOOT``) so the two never double-respawn.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_AIOHTTP_IMPORT_ERROR: Exception | None = None
try:  # aiohttp is a matrix-extra dependency; the DAEMON requires it, but
    # pure helpers (render_sidecar_unit, re-exported from config_gen) must
    # stay importable without it so provision.ensure_sidecar_unit can
    # install the unit file on a fresh install before the crypto stack
    # (which includes aiohttp) exists. Never raise here — daemon entry
    # points call _require_aiohttp() instead.
    from aiohttp import web
except ImportError as exc:
    web = None  # type: ignore[assignment]
    _AIOHTTP_IMPORT_ERROR = exc


def _require_aiohttp() -> None:
    """Raise the daemon's missing-dep SystemExit (lazy, not at import)."""
    if web is None or _AIOHTTP_IMPORT_ERROR is not None:
        raise SystemExit(
            "observatory sidecar requires aiohttp (matrix extra)") from _AIOHTTP_IMPORT_ERROR

from observatory import e2ee as e2ee_mod
from observatory import provision
from observatory.config_gen import (
    APPSERVICE_PORT_DEFAULT,
    HOMESERVER_ADDRESS,
    HOMESERVER_UNIT_NAME,
    ObservatoryPaths,
    render_sidecar_unit,
)
from observatory.identity import assign_slug, virtual_mxid
from observatory.control import QUEUED_STEER_NOTICE, AbortSession, InjectText, OmpAbortMain, OmpPrompt, OmpSteer, OmpSubagentAbort, OmpSubagentSteer, ResolveApproval, STOP_CONFIRMED_NOTICE
from observatory.gateway_transport import (
    ControlSocketGatewayTransport,
    GatewayTransportError,
    gateway_progress_sock_path,
)
from observatory.renderer import EditMessage, IntentExecutor, Renderer, SendMessage
from observatory.state import ObservatoryState, StateError
from observatory.tree import DIRECTIVES_ROOM_KEY

try:
    from observatory.appservice import (
        TransactionIntake,
        as_token_from_registration,
        make_app,
    )
    from observatory.matrix_client import CLIENT_V3, MatrixError, MatrixClient
except ImportError as exc:  # aiohttp (or another matrix-extra dep) missing
    if _AIOHTTP_IMPORT_ERROR is None:
        _AIOHTTP_IMPORT_ERROR = exc
    TransactionIntake = None  # type: ignore[assignment]
    as_token_from_registration = None  # type: ignore[assignment]
    make_app = None  # type: ignore[assignment]
    CLIENT_V3 = ""  # type: ignore[assignment]
    MatrixError = Exception  # type: ignore[assignment]
    MatrixClient = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: Stable node id of the gateway agent (depth 0, extra.kind="gateway").
#: render_live.seed_nodes / spawn.py convention; restart-immune (D18):
#: re-ensured on every boot, never re-created when present.
GATEWAY_NODE_ID = "gw"
GATEWAY_NODE_NAME = "gateway agent"

#: Background loop cadences (seconds).
APPROVALS_EXPIRY_INTERVAL = 5.0
CRON_POLL_INTERVAL = 5.0
MANUAL_POLL_INTERVAL = 5.0
#: Admin-token self-heal cadence (defect vi): re-validate the owner admin
#: token and re-login when stale, so purges never 401 mid-operation.
ADMIN_TOKEN_HEAL_INTERVAL = 15.0 * 60.0
#: Gateway-room prompt delivery is prompt-only: plain text starts a turn
#: on the live gateway session (there is no busy run to steer into), so
#: the router's "queued steer" honesty notice never applies there — the
#: reply itself is the acknowledgement.
#: Delivery/reporting notices posted in the gateway agent's voice.
GATEWAY_UNREACHABLE_NOTICE = (
    "⚠ gateway is not answering — message not delivered "
    "(start the gateway and retry)"
)
GATEWAY_PROMPT_FAILED_NOTICE = (
    "⚠ gateway prompt failed — see the sidecar log"
)
#: Long-turn liveness: one still-working notice after this many seconds,
#: cancelled on completion, so long turns are never silence.
GATEWAY_PROMPT_LIVENESS_AFTER_S = 90.0
GATEWAY_PROMPT_WORKING_NOTICE = (
    "… still working — long turn in progress, reply to follow"
)
#: Spawned-child delivery notices (spawn-silent fix), posted in the
#: child's own voice when its engine handle cannot be reached or a turn
#: fails. Replies themselves render via the renderer, never these.
CHILD_UNAVAILABLE_NOTICE = (
    "⚠ this room's session is unavailable — restart the sidecar to resume it."
)
CHILD_PROMPT_FAILED_NOTICE = (
    "⚠ prompt failed — see the sidecar log"
)
CHILD_STEER_FAILED_NOTICE = (
    "⚠ steer failed — see the sidecar log"
)
#: BUG2 follow-up spam gate: injected delegate summaries are truncated to
#: this many chars (the gateway turn sees the gist, the room stays quiet).
FOLLOWUP_SUMMARY_MAX_CHARS = 500
#: Routine-success markers: a delegate summary carrying one of these and NO
#: question/failure signal is maintenance noise (rotation/verify/complete)
#: with nothing for the owner — the follow-up inject is skipped.
FOLLOWUP_ROUTINE_MARKERS = ("verified", "complete", "completed", "success", "rotated", "updated", "done", "ok", "passed")
FOLLOWUP_ATTENTION_MARKERS = ("fail", "error", "question", "help", "approve", "approval", "blocked", "needs", "todo", "fix", "?")

#: Live-ingest datagram cap (unix SOCK_DGRAM payload ceiling).
GATEWAY_LIVE_DATAGRAM_MAX = 65535

#: /cot status (§5.2, default OFF): Telegram-shaped single status per
#: gateway turn. Short replies seal by editing the status into the reply;
#: longer replies leave the last status and send separately.
COT_STATUS_SEAL_SHORT_MAX_CHARS = 280


def _cot_thinking_faces() -> list:
    """Out-of-box faces (display.py KawaiiSpinner.get_thinking_faces)."""
    for holder in ("Display", "KawaiiSpinner"):
        try:
            mod = __import__("agent.display", fromlist=[holder])
            cls = getattr(mod, holder, None)
            if cls is None:
                continue
            get = getattr(cls, "get_thinking_faces", None)
            if callable(get):
                faces = get()
                if faces:
                    return list(faces)
            base = list(getattr(cls, "KAWAII_THINKING", []) or [])
            if base:
                return base
        except Exception:
            continue
    return []


def _cot_thinking_verbs() -> list:
    """Out-of-box verbs (display.py KawaiiSpinner.get_thinking_verbs)."""
    for holder in ("Display", "KawaiiSpinner"):
        try:
            mod = __import__("agent.display", fromlist=[holder])
            cls = getattr(mod, holder, None)
            if cls is None:
                continue
            get = getattr(cls, "get_thinking_verbs", None)
            if callable(get):
                verbs = get()
                if verbs:
                    return list(verbs)
            base = list(getattr(cls, "THINKING_VERBS", []) or [])
            if base:
                return base
        except Exception:
            continue
    return []


def cot_status_text(seq: int = 0) -> str:
    """One status line: face + verb + ellipsis, deterministic by seq.

    Strings come ONLY from display.py thinking faces/verbs (never invented);
    the pick cycles by seq (no random import).
    """
    faces = _cot_thinking_faces()
    verbs = _cot_thinking_verbs()
    if not faces or not verbs:
        return "…"
    try:
        idx = int(seq)
    except Exception:
        idx = 0
    return f"{faces[idx % len(faces)]} {verbs[idx % len(verbs)]}…"


def cot_status_seal_short(reply: str, max_chars: int = COT_STATUS_SEAL_SHORT_MAX_CHARS) -> bool:
    """True when a final reply is short enough to seal the status into."""
    try:
        stripped = (reply or "").strip()
    except Exception:
        return False
    if not stripped:
        return False
    try:
        limit = int(max_chars)
    except Exception:
        limit = COT_STATUS_SEAL_SHORT_MAX_CHARS
    return len(stripped) <= limit


def _needs_room_reply(summary: Any, status: str = "") -> bool:
    """BUG2 needs-room-reply gate: False = routine success, skip the follow-up.

    A delegate death needs a gateway turn only when the owner should see
    something: failures/errors always qualify, as does any question/failure
    marker in the summary. A summary that carries a routine-success marker
    (verified/complete/…) and NO attention marker is maintenance noise —
    skip it. Empty summaries carry nothing — skip those too.
    """
    text = str(summary or "").strip()
    if str(status or "").strip().lower() not in ("", "completed", "complete", "success", "ok", "done"):
        return True
    if not text:
        return False
    lowered = text.lower()
    has_attention = any(marker in lowered for marker in FOLLOWUP_ATTENTION_MARKERS)
    if has_attention:
        return True
    return not any(marker in lowered for marker in FOLLOWUP_ROUTINE_MARKERS)


def _coerce_live_seq(value: Any) -> int | None:
    """Coerce a datagram/event seq to int; None when absent/invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    return None


def _live_event_args_text(args: Any) -> str | None:
    """Event args → renderer text (same shape as batched replay)."""
    if args is None:
        return None
    if isinstance(args, str):
        return args or None
    if isinstance(args, dict):
        try:
            return json.dumps(args, default=str)
        except Exception:
            return str(args)
    return str(args)

SMOKE_MARKER = "MERCURY-M4C-OK"
SMOKE_E2EE_MARKER = "MERCURY-M4C-E2EE-OK"
SMOKE_FAIL_MARKER = "MERCURY-M4C-FAIL"
DEFAULT_SMOKE_HOME = Path.home() / ".mercury" / "observatory-build" / "sidecar-smoke"

# (render_sidecar_unit lives in observatory.config_gen — pure templating
# with no aiohttp dependency — and is re-exported from this module's
# imports above for back-compat.)


# ============================================================================
# The daemon
# ============================================================================

class SidecarDaemon:
    """Owns every observatory subsystem's lifecycle in one asyncio loop."""

    def __init__(
        self,
        mercury_home: str | Path,
        *,
        hermes_db: str | Path | None = None,
        poll_interval: float = 2.0,
        appservice_port: int = APPSERVICE_PORT_DEFAULT,
        e2ee: bool | None = None,
        systemd: bool = True,
        boot_homeserver: bool = True,
    ) -> None:
        self.mercury_home = Path(mercury_home)
        self.paths = ObservatoryPaths(self.mercury_home)
        self.hermes_db = (
            Path(hermes_db) if hermes_db is not None
            else self.mercury_home / "hermes" / "state.db"
        )
        self.poll_interval = poll_interval
        self.appservice_port = appservice_port
        self.e2ee_flag = e2ee_mod.e2ee_enabled(self.mercury_home) if e2ee is None else e2ee
        self.systemd = systemd
        self.boot_homeserver = boot_homeserver

        # Components assembled by boot(); typed None until then.
        self.state: ObservatoryState | None = None
        self.client: MatrixClient | None = None
        self.renderer: Renderer | None = None
        self.executor: IntentExecutor | Any = None
        self.e2ee: e2ee_mod.E2EEManager | None = None
        self.intake: TransactionIntake | None = None
        self._runner: Any = None  # web.AppRunner once aiohttp is required at boot
        self.discovery: Any | None = None
        self.registry: Any | None = None
        # Sibling subsystems (M4a/M4b/M5).
        self.control_router: Any | None = None
        self.approvals: Any | None = None
        self.directives: Any | None = None
        self.manual_runs: Any | None = None
        self.cron_rooms: Any | None = None
        #: Gateway-session prompt transport (control-socket ``inject``).
        #: None only when the transport package is unavailable — delivery
        #: then reports unreachable instead of dying silently.
        self.gateway_transport: Any | None = None
        #: In-flight gateway-room prompt deliveries (a set so shutdown
        #: cancellation is trivial).
        self._gateway_tasks: set[asyncio.Task] = set()
        #: Per-node seqs already rendered live via the gateway-progress
        #: datagram socket. Cleared at each _deliver_gateway_prompt start;
        #: _replay_gateway_events skips batched events whose seq is in here
        #: (events without seq always render). Best-effort: bind failure
        #: disables live only.
        self._gateway_live_seqs: dict[str, set[int]] = {}
        #: BUG2: nodes with an internal follow-up turn in flight. Live
        #: per-event renders collapse to ONE liveness notice while set;
        #: replay still records the events for logs without room sends.
        self._gateway_internal_turns: dict[str, bool] = {}
        #: /cot status (default OFF): in-flight gateway-turn status event id
        #: per node (original send id; edits always target it). Posted at
        #: turn start, edited in place on thinking, sealed at turn end.
        self._cot_status_event: dict[str, str] = {}
        #: Live-ingest unix datagram socket (None when disabled/failed).
        self._gateway_live_sock: Any = None
        self._gateway_live_enabled: bool = False
        #: Grandchild node map for datagram-forwarded child feeds:
        #: child node_id -> subagent_id -> grandchild node_id (mirrors the
        #: per-feed map in _run_omp_feed for registry children).
        self._datagram_grandchildren: dict[str, dict[str, str]] = {}
        #: Rooms already carrying a decrypt-failure recovery notice (one
        #: notice per room per process — failures after the first only log).
        self._decrypt_notified: set[str] = set()
        self.omp_feeds: dict[str, Any] = {}  # node_id -> OmpFeed
        #: Spawned-child delivery (spawn-silent fix): in-flight child-turn
        #: tasks (drained/cancelled like ``_gateway_tasks``), per-node
        #: turn locks (one hermes turn at a time per child), and the
        #: busyness set the control router's ``busy_probe`` reads (an omp
        #: main with a turn in flight takes ``steer``; an idle one takes
        #: ``prompt`` — without this every idle omp child steered into
        #: the void and never answered).
        self._child_tasks: set[asyncio.Task] = set()
        self._child_locks: dict[str, asyncio.Lock] = {}
        self._child_busy: set[str] = set()

        self._homeserver_proc: subprocess.Popen | None = None
        self._discovery_task: asyncio.Task | None = None
        self._loops: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        self.base_url = ""
        self.server_name = ""
        self.owner_mxid = ""
        self.admin_token = ""
        self.gateway_mxid = ""

        # Observability for tests/smoke: last events the intake consumed
        # and the routing dispositions the control router produced.
        self.seen_events: list[dict[str, Any]] = []
        self.routing_log: list[str] = []

    # --- homeserver ----------------------------------------------------------

    def _homeserver_healthy(self) -> bool:
        import urllib.request

        if not self.base_url:
            return False
        try:
            with urllib.request.urlopen(
                f"{self.base_url}/_matrix/client/versions", timeout=2
            ) as resp:
                return resp.status == 200
        except OSError:
            return False

    def _start_homeserver_unit(self) -> bool:
        """systemctl --user start (idempotent); True when issued."""
        import shutil

        if not self.systemd or shutil.which("systemctl") is None:
            return False
        try:
            subprocess.run(
                ["systemctl", "--user", "start", HOMESERVER_UNIT_NAME],
                check=True, capture_output=True, timeout=30,
            )
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("systemctl start %s failed: %s", HOMESERVER_UNIT_NAME, exc)
            return False

    def _spawn_homeserver(self) -> None:
        """Boot the binary as OUR child (no systemd / smoke). Owned for
        shutdown."""
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        server_log = (self.paths.logs_dir / "tuwunel-sidecar.log").open("wb")
        self._homeserver_proc = subprocess.Popen(
            [str(self.paths.binary), "-c", str(self.paths.toml)],
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        log.info("booted tuwunel pid %s (config %s)", self._homeserver_proc.pid, self.paths.toml)

    async def _ensure_homeserver(self, timeout: float = 120.0) -> None:
        if self._homeserver_healthy():
            log.info("homeserver already up at %s", self.base_url)
            return
        if not self.boot_homeserver:
            raise provision.ProvisionError(
                f"homeserver not reachable at {self.base_url} and boot disabled"
            )
        if not self._start_homeserver_unit():
            self._spawn_homeserver()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._homeserver_healthy():
                return
            await asyncio.sleep(0.5)
        raise provision.ProvisionError(
            f"homeserver did not become healthy at {self.base_url} within {timeout:.0f}s"
        )

    # --- gateway node (D18: restart is not death) ------------------------------

    def ensure_gateway_node(self) -> str:
        """Idempotent gateway agent row; returns its mxid. ``extra.kind``
        is what respawn/spawn/scan key on — the node id is ours. The
        node's ``space_id`` stays the ROOT space; the gateway agent's own
        subspace (gw-space parity) is provisioned by the boot/respawn
        ``apply_plan`` pass and persists in state meta under
        ``space:gw-agent``."""
        assert self.state is not None
        try:
            return str(self.state.get(GATEWAY_NODE_ID)["mxid"])
        except StateError:
            slug = assign_slug(GATEWAY_NODE_NAME, self.state)
            mxid = virtual_mxid(slug, server_name=self.server_name)
            self.state.add_node(
                GATEWAY_NODE_ID,
                engine="hermes",
                name=GATEWAY_NODE_NAME,
                slug=slug,
                mxid=mxid,
                session_ref="session:gateway",
                parent_node_id=None,
                extra={"kind": "gateway"},
            )
            log.info("gateway node created: %s (%s)", GATEWAY_NODE_ID, mxid)
            return mxid

    # --- boot -------------------------------------------------------------------

    async def boot(self) -> dict[str, Any]:
        """Full assembly; returns a boot report (evidence for logs/smoke)."""
        _require_aiohttp()
        report: dict[str, Any] = {"home": str(self.mercury_home), "e2ee": self.e2ee_flag}

        # 1. provision (idempotent, offline-aware)
        summary = provision.provision(mercury_home=self.mercury_home, systemd=self.systemd)
        report["provision"] = summary

        # runtime config (address/port/server_name from the closed toml)
        import tomllib

        with open(self.paths.toml, "rb") as f:
            cfg = tomllib.load(f)["global"]
        address = cfg.get("address", HOMESERVER_ADDRESS)
        if isinstance(address, list):
            address = address[0]
        port = int(cfg.get("port", 18008))
        self.base_url = self.paths.homeserver_url(address=address, port=port)
        self.server_name = str(cfg.get("server_name", "mercury.local"))
        report["base_url"] = self.base_url

        # 2. homeserver up
        await self._ensure_homeserver()

        # 3. state + gateway node
        self.state = ObservatoryState(self.paths.root / "state.db")
        self.owner_mxid, self.admin_token = self._load_owner()
        self.gateway_mxid = self.ensure_gateway_node()
        report["gateway_mxid"] = self.gateway_mxid

        # 4. client + (flag-gated) E2EE executor + renderer
        self.client = MatrixClient(
            self.base_url,
            as_token_from_registration(self.paths.appservice_registration),
            server_name=self.server_name,
            admin_token=self.admin_token,
            on_admin_401=self._refresh_admin_token_now,
        )
        self.executor = await self._build_executor()
        self.renderer = Renderer(
            self.state,
            gateway_node_id=GATEWAY_NODE_ID,
            server_name=self.server_name,
            owner_mxid=self.owner_mxid,
            executor=self.executor,
        )

        # 5. D18: respawn BEFORE serving traffic (adopt the gateway-thread
        #    boot's registry when one exists — never double-respawn)
        report["respawn"] = await self._run_respawn_pass()

        # 6. converge the space tree (virtual users + plan)
        await self._ensure_virtual_users()
        await self.verify_gateway_ghost()
        report["gateway_ghost"] = "verified"
        applied = await self.renderer.apply_plan(
            self.renderer.build_plan(host=socket.gethostname())
        )
        report["apply_plan"] = len(applied)
        # Owner auto-join heal (VM round 2): every tracked room/space the
        # owner hasn't joined yet joins now with the owner's own credential
        # (same POST /join as a Join tap) — no more invite prompts on
        # owner-owned rooms. Best-effort; boot never fails on it.
        try:
            report["owner_joined"] = await self.executor.ensure_owner_in_plan(
                self.renderer.build_plan(host=socket.gethostname()))
        except Exception as exc:  # noqa: BLE001 — membership heal never fails boot
            log.warning("owner membership heal skipped: %s", exc)
            report["owner_joined"] = 0

        # VM round 3 — first-login cold start: the first E2EE share + power
        # snapshot above were built BEFORE the owner ever joined (this heal
        # runs at boot when the owner has no session). When the heal joined
        # anything, drop every pre-join outbound Megolm session so the next
        # send re-shares fresh (fresh TOFU trust + fresh OTK verify) — no
        # pre-join session is ever reused.
        try:
            live_rows = self.state.get_live()
            live_rooms = sorted({
                rid for row in live_rows
                for rid in (row.get("room_id"), row.get("space_id")) if rid
            })
            directives_room = self._directives_room_id()
            if directives_room and directives_room not in live_rooms:
                live_rooms.append(directives_room)
            live_senders = sorted({
                str(row.get("mxid") or "") for row in live_rows
                if str(row.get("mxid") or "")
            })
            if self.gateway_mxid and self.gateway_mxid not in live_senders:
                live_senders.append(self.gateway_mxid)
            report["megolm_rotated_rooms"] = len(live_rooms)
            rotated: dict[str, int] = {}
            if report.get("owner_joined") and self.e2ee is not None:
                rotated = await self.e2ee.drop_outbound_sessions(
                    live_rooms, senders=live_senders)
            report["megolm_rotated"] = rotated
            log.info("post-join heal: joined=%s rotated=%s over %d live room(s)",
                     report.get("owner_joined"), rotated, len(live_rooms))
        except Exception as exc:  # noqa: BLE001 — rotation never fails boot
            log.warning("post-join rotation skipped: %s", exc)
            report["megolm_rotated"] = {}

        # VM round 3 — stale OTK after annihilate: the NEXT boot after a
        # wipe force-drops ALL outbound Megolm sessions (the crypto dir may
        # have survived a partial wipe with pre-wipe sessions) + re-runs
        # owner trust fresh per sender with no snapshot to compare against
        # (first sight). One-shot: the marker is unlinked after use.
        try:
            marker = self.paths.root / provision.POST_WIPE_MARKER_NAME
            if marker.exists():
                if self.e2ee is not None:
                    live_rows2 = self.state.get_live()
                    rooms2 = sorted({
                        rid for row in live_rows2
                        for rid in (row.get("room_id"), row.get("space_id")) if rid
                    })
                    senders2 = sorted({
                        str(row.get("mxid") or "") for row in live_rows2
                        if str(row.get("mxid") or "")
                    })
                    if self.gateway_mxid and self.gateway_mxid not in senders2:
                        senders2.append(self.gateway_mxid)
                    report["post_wipe_rotation"] = await self.e2ee.post_wipe_rotation(
                        rooms2, senders2)
                else:
                    report["post_wipe_rotation"] = {
                        "dropped": {}, "trust": {}, "e2ee": "off"}
                try:
                    marker.unlink()
                    report["post_wipe_consumed"] = True
                except Exception:  # noqa: BLE001 — stale marker retries next boot
                    report["post_wipe_consumed"] = False
                    log.warning("post-wipe marker unlink failed: %s", marker)
                log.info("post-wipe rotation consumed: %s",
                         report.get("post_wipe_rotation"))
            else:
                report["post_wipe_rotation"] = {}
        except Exception as exc:  # noqa: BLE001 — post-wipe hook never fails boot
            log.warning("post-wipe rotation skipped: %s", exc)
            report["post_wipe_rotation"] = {}
        # 7. sibling subsystems (M4a/M4b/M5) — integrate, never edit
        self.wire_siblings()
        # directives membership reconcile + D7 snapshot warm (BEFORE the
        # intake serves: first steer must not fail-closed on a cold cache).
        # AFTER the owner-join heal above — a pre-join snapshot misses the
        # owner and reads stale until the next tick (first-login cold start).
        await self._sync_directives_membership()
        await self._refresh_power_levels()
        report["directives_members"] = len(self._directives_members())
        report["power_rooms"] = len(self._pl_cache)

        # 8. intake endpoint (this loop) — LAST: traffic only after recovery
        await self._serve_intake()
        # 8b. live ingest: unix datagram socket for gateway turn progress
        #     + gateway-child feed frames. Best-effort: bind failure
        #     disables live only — batched replay still works.
        self._start_gateway_live_listener()
        report["gateway_live"] = bool(self._gateway_live_enabled)
        # 8c. omp feeds for spawned children (registry). Gateway-origin
        #     children arrive cross-process via the live socket (8b) — the
        #     sidecar never imports the gateway's in-process table.
        self._attach_omp_feeds()
        # 9. discovery (§7) + background loops
        self._start_discovery()
        self._start_loops()

        log.info(
            "observatory sidecar up: %s (e2ee=%s, applied=%d, resumed=%s)",
            self.base_url, self.e2ee_flag, len(applied),
            report["respawn"].get("resumed", []),
        )
        return report

    async def _build_executor(self) -> Any:
        assert self.client is not None and self.state is not None
        if not self.e2ee_flag:
            return IntentExecutor(
                self.client,
                self.state,
                owner_mxid=self.owner_mxid,
                server_name=self.server_name,
                gateway_mxid=self.gateway_mxid,
            )
        self.e2ee = e2ee_mod.E2EEManager(
            self.client,
            self.state,
            crypto_dir=e2ee_mod.crypto_dir_for(self.mercury_home),
            owner_mxid=self.owner_mxid,
            gateway_mxid=self.gateway_mxid,
        )
        # fails HARD when the crypto stack is missing (O3 — never fake it)
        await self.e2ee.start(enabled=True)
        try:  # publish device + one-time keys NOW (warmup): a cold key
            # directory is exactly the "other party is currently not logged
            # in" FluffyChat dead-end. A failed warmup only logs — every
            # later send/load retries the upload, never silent plaintext.
            warmed = await self.e2ee.warmup()
            log.info("e2ee warmup published keys for %s", sorted(warmed))
        except Exception:  # noqa: BLE001 — boot must survive HS blips
            log.warning("e2ee warmup failed — keys publish on next use", exc_info=True)
        return e2ee_mod.EncryptedIntentExecutor(
            self.client,
            self.state,
            owner_mxid=self.owner_mxid,
            server_name=self.server_name,
            gateway_mxid=self.gateway_mxid,
            e2ee=self.e2ee,
        )

    async def _run_respawn_pass(self) -> dict[str, Any]:
        """D18: purge-journal replay + 0-agent resume, BEFORE traffic.
        Adopts ``platform_hook.LAST_BOOT``'s registry (the gateway thread
        may have booted already) so handles are never double-owned."""
        from observatory import platform_hook
        from observatory.respawn import respawn_pass
        from observatory.spawn import OrchestratorRegistry

        last = getattr(platform_hook, "LAST_BOOT", None)
        self.registry = getattr(last, "registry", None) or OrchestratorRegistry()
        try:
            result = await respawn_pass(
                state=self.state,
                registry=self.registry,
                renderer=self.renderer,
                mercury_home=self.mercury_home,
            )
            return result.as_dict()
        except Exception as exc:  # noqa: BLE001 — respawn reports, never blocks boot
            log.exception("respawn pass failed (continuing — D18 best-effort)")
            return {"error": str(exc)}

    def _load_owner(self) -> tuple[str, str]:
        doc = json.loads(self.paths.owner_credentials.read_text(encoding="utf-8"))
        return str(doc["user_id"]), str(doc.get("access_token") or "")

    async def _refresh_admin_token_now(self) -> str | None:
        """Re-login the owner admin token NOW (defect vi re-login path).

        Returns the fresh token (also reloaded into self + client) or
        None when the heal is unavailable/failed — the caller's original
        401 then stands. Never raises."""
        try:
            from observatory.provision import heal_owner_admin_token

            outcome = heal_owner_admin_token(self.paths)
            owner_mxid, admin_token = self._load_owner()
            self.owner_mxid, self.admin_token = owner_mxid, admin_token
            if self.client is not None:
                self.client.admin_token = admin_token
            log.info("admin token self-heal: %s", outcome)
            return admin_token or None
        except Exception:  # noqa: BLE001 — heal failure = original error stands
            log.exception("admin token self-heal failed")
            return None

    async def _admin_token_tick(self) -> None:
        """Periodic self-heal (defect vi): validate + refresh on stale."""
        await self._refresh_admin_token_now()

    async def _ensure_virtual_users(self) -> None:
        assert self.client is not None and self.state is not None
        for row in self.state.get_live():
            localpart = row["mxid"].lstrip("@").split(":", 1)[0]
            try:
                await self.client.register_virtual_user(localpart)
            except MatrixError as exc:
                log.info(
                    "register %s: %s (continuing — ghost may auto-provision)",
                    localpart, exc,
                )

    # --- gateway ghost verification (VM-feedback: never serve a dead tree) ----

    async def ghost_exists(self, mxid: str) -> bool:
        """True when the homeserver knows this ghost.

        Contract: ``GET /_matrix/client/v3/profile/{userId}`` → 200 means
        the ghost exists; 404/``M_NOT_FOUND`` means it does not. Any other
        :class:`MatrixError` propagates — a sick homeserver must not read
        as "ghost missing".
        """
        assert self.client is not None
        from urllib.parse import quote

        try:
            await self.client.client_api(
                "GET", f"{CLIENT_V3}/profile/{quote(mxid, safe='')}"
            )
        except MatrixError as exc:
            if exc.status == 404 or exc.errcode == "M_NOT_FOUND":
                return False
            raise
        return True

    async def verify_ghost(self, mxid: str) -> None:
        """Register-once-then-verify one ghost; FAIL LOUDLY when still missing.

        The ``_ensure_virtual_users`` pass stays best-effort (a ghost may
        legitimately auto-provision on its first masqueraded call), but a
        ghost that is still unknown after an explicit re-register means
        boot would serve a dead tree — raise :class:`ProvisionError`
        instead. Repair path: :meth:`repair_ghosts` /
        ``--repair-ghosts``.
        """
        assert self.client is not None
        if await self.ghost_exists(mxid):
            return
        localpart = mxid.lstrip("@").split(":", 1)[0]
        try:
            await self.client.register_virtual_user(localpart)
        except MatrixError as exc:
            log.warning("ghost re-register %s failed: %s", localpart, exc)
        if not await self.ghost_exists(mxid):
            raise provision.ProvisionError(
                f"ghost {mxid} missing on the homeserver after re-register — "
                "refusing to serve a dead tree. Re-run the repair path "
                "(`mercury setup observatory`, Install/repair) or "
                "`python -m observatory.sidecar_main --repair-ghosts`, "
                "then restart the sidecar."
            )

    async def verify_gateway_ghost(self) -> None:
        """Boot gate: the gateway ghost MUST exist before traffic is served."""
        if not self.gateway_mxid:
            raise provision.ProvisionError(
                "gateway mxid unset at ghost-verify time — refusing to boot"
            )
        await self.verify_ghost(self.gateway_mxid)

    async def repair_ghosts(self) -> dict[str, str]:
        """Re-register every live ghost and verify each (repair path).

        Returns ``{mxid: status}`` with status ``"verified"``,
        ``"missing"`` (register accepted but the ghost is still unknown),
        ``"register-failed: ..."`` or ``"verify-failed: ..."``. Never
        raises per-ghost — the caller decides (boot fails loudly via
        :meth:`verify_gateway_ghost`; ``--repair-ghosts`` exits non-zero
        unless every ghost verifies).
        """
        assert self.client is not None and self.state is not None
        results: dict[str, str] = {}
        for row in self.state.get_live():
            mxid = str(row["mxid"])
            localpart = mxid.lstrip("@").split(":", 1)[0]
            try:
                await self.client.register_virtual_user(localpart)
            except Exception as exc:  # noqa: BLE001 — repair reports, never crashes per-ghost
                log.warning("ghost repair register %s failed: %s", localpart, exc)
                results[mxid] = f"register-failed: {exc}"
                continue
            try:
                exists = await self.ghost_exists(mxid)
            except MatrixError as exc:
                results[mxid] = f"verify-failed: {exc}"
                continue
            results[mxid] = "verified" if exists else "missing"
        return results

    async def _serve_intake(self) -> None:
        _require_aiohttp()
        assert self.client is not None
        # Homeserver→AS transactions authenticate with the hs_token (the
        # registration's homeserver-side secret — "hs_token authenticates
        # the homeserver's transactions TO the sidecar", config_gen).
        # TransactionIntake's constructor param is named ``as_token`` (M3a
        # naming); the VALUE the wire protocol validates is hs_token.
        self.intake = TransactionIntake(
            as_token=hs_token_from_registration(self.paths.appservice_registration),
            handler=self._on_transaction,
            crypto_handler=self._on_crypto,
        )
        # MSC3984 (HS fans @merc_* key queries/claims here): serve REAL key
        # material from the E2EE machines. E2EE off (self.e2ee None) leaves
        # the routes detached — they 404 honestly instead of faking keys.
        if self.e2ee is not None:
            self.intake.attach_key_query_handler(self.e2ee.key_query)
            self.intake.attach_key_claim_handler(self.e2ee.key_claim)
        app = make_app(self.intake)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, HOMESERVER_ADDRESS, self.appservice_port)
        await site.start()
        await self.intake.start()
        log.info("appservice intake listening on %s:%d", HOMESERVER_ADDRESS, self.appservice_port)

    # --- sibling subsystems (M4a control/approvals, M5 spawn/rooms) -------------

    def wire_siblings(self) -> None:
        """Construct the parallel-wave subsystems against their landed
        interfaces (each module owns its file; this daemon imports and
        never edits). Gateway-node prompts deliver over the gateway
        control socket (``inject``); the remaining engine transports
        (RPC steer fan-out, aborts) are not landed — those ACTIONS stay
        logged in ``routing_log``.

        M4b approval ingest is wired here too (see
        :meth:`_wire_approval_ingest`): every guard prompt becomes a room
        prompt in the bridge (the approval source of truth), and room
        /approve|/deny resolves the exact queue the blocked turn waits on
        — same-process queues directly, gateway-process queues over the
        control socket ``resolve-approval`` verb."""
        from observatory.approvals import ApprovalBridge, MatrixAuthority
        from observatory.control import ControlRouter, RoomPowerLevels
        from observatory.cron_rooms import CronRooms, CronStore
        from observatory.directives import DirectivesManager
        from observatory.manual_runs import ManualRunsWatcher

        assert self.state is not None and self.renderer is not None and self.client is not None

        # D7 authority gate: the router reads power levels SYNCHRONOUSLY
        # (PowerLevelProvider contract) — the daemon supplies a live
        # snapshot cache (PowerLevelSnapshot pattern, control.py's own
        # docstring), warmed at boot, kept fresh by transaction events
        # (m.room.power_levels / membership) + the periodic refresh tick.
        # A room missing from the snapshot reads as None — the router
        # fails CLOSED, never guesses.
        self._pl_cache = {}

        def pl_snapshot(room_id: str):
            return self._pl_cache.get(room_id)

        def _child_busy_probe(node_id: str) -> bool:
            # Omp mains: steer mid-run, prompt a new turn when idle. The
            # set holds exactly the nodes with a child-turn task in
            # flight (marked synchronously at dispatch, cleared at task
            # end) — without a probe the router assumes busy and an idle
            # spawned omp child steers into the void forever.
            return node_id in self._child_busy

        self.control_router = ControlRouter(
            self.state,
            gateway_node_id=GATEWAY_NODE_ID,
            pl_provider=pl_snapshot,
            busy_probe=_child_busy_probe,
        )
        # Gateway-session prompt transport FIRST: the approval resolver
        # below forwards cross-process resolutions over it.
        # Construction is side-effect free (no I/O until a prompt sends);
        # None only when the package itself is unavailable.
        try:
            self.gateway_transport = ControlSocketGatewayTransport(self.mercury_home)
        except Exception:  # noqa: BLE001 — delivery reports unreachable instead
            log.exception("gateway transport unavailable (prompts will not deliver)")
            self.gateway_transport = None
        self.approvals = ApprovalBridge(
            state=self.state,
            poster=self.client,
            authority=MatrixAuthority(self.client, reader_mxid=self.gateway_mxid),
            resolve_gateway=self._resolve_gateway_approval,
        )
        self._wire_approval_ingest()
        self.directives = DirectivesManager(self.renderer)
        self.cron_rooms = CronRooms(
            self.renderer,
            store=CronStore.for_hermes_home(self.mercury_home / "hermes"),
        )
        self.manual_runs = ManualRunsWatcher(
            self.renderer, agent_dir=self.mercury_home / "omp",
            # observatory.mirror_cli (default off): CLI/manual TUI sessions
            # never get rooms unless the operator opts into observe/full.
            mode=provision.mirror_cli_mode(self.mercury_home),
        )

    def _wire_approval_ingest(self) -> None:
        """Feed every approval stream into the bridge (never raises).

        - gateway hermes turns (THIS process): ``gateway_notify`` under the
          canonical :data:`gateway_session.GATEWAY_APPROVAL_SESSION_KEY`
          (the key gateway Matrix turns block on — one shared constant,
          never recomputed per side);
        - gateway Matrix turns (GATEWAY process): ``approval_prompt``
          datagrams → ``bridge.submit`` (see
          :meth:`_handle_approval_prompt_datagram`);
        - sidecar omp children: the global approval-frame hook routes each
          frame to its room (see :meth:`_omp_approval_frame_router`).

        The control router's own approval table is deliberately NOT fed:
        the bridge owns resolution and its resolved notice; feeding both
        would double-post every decision (the stale router no-pending
        notice is suppressed instead — see ``_route_inbound``).
        Re-wire is idempotent (previous registrations replaced)."""
        from observatory.gateway_session import GATEWAY_APPROVAL_SESSION_KEY

        bridge = self.approvals
        if bridge is None:
            return
        try:
            bridge.capture_loop()
        except RuntimeError:
            pass  # no running loop (unit tests) — ingest captures on first use
        try:
            from tools.approval import register_gateway_notify, unregister_gateway_notify
        except Exception:
            register_gateway_notify = None  # type: ignore[assignment]
            unregister_gateway_notify = None  # type: ignore[assignment]
        if register_gateway_notify is not None:
            try:
                from observatory.approvals import gateway_notify
                if unregister_gateway_notify is not None:
                    try:
                        unregister_gateway_notify(GATEWAY_APPROVAL_SESSION_KEY)
                    except Exception:
                        pass
                register_gateway_notify(
                    GATEWAY_APPROVAL_SESSION_KEY,
                    gateway_notify(bridge, GATEWAY_NODE_ID, GATEWAY_APPROVAL_SESSION_KEY),
                )
            except Exception:
                log.exception("approval ingest: gateway notify not registered")
        try:
            from tools.omp_rpc_transport import set_approval_frame_hook
        except Exception:
            set_approval_frame_hook = None  # type: ignore[assignment]
        if set_approval_frame_hook is not None:
            try:
                set_approval_frame_hook(self._omp_approval_frame_router)
            except Exception:
                log.exception("approval ingest: frame hook not registered")

    async def _resolve_gateway_approval(
        self,
        session_key: str,
        choice: str,
        request_id: str,
        reason: str | None = None,
    ) -> int:
        """ApprovalBridge gateway resolver: same-process queue first, else
        forward to the gateway process over the control socket
        (``resolve-approval`` verb). A forward failure (gateway down,
        unknown verb, timeout) RAISES so the bridge keeps the pending for
        retry (its ``error:resolver`` path); 0 is returned only when a
        resolver answered that nothing is pending (the bridge takes its
        ``late`` path)."""
        try:
            from tools.approval import resolve_gateway_approval as _local_resolve
        except Exception:
            _local_resolve = None  # type: ignore[assignment]
        if _local_resolve is not None:
            try:
                settled = int(
                    _local_resolve(
                        session_key, choice,
                        reason=reason, request_id=request_id or None,
                    ) or 0
                )
            except Exception:
                log.exception("approval resolve: local resolver failed")
                settled = 0
            if settled:
                return settled
        transport = self.gateway_transport
        resolve_fn = getattr(transport, "resolve_approval", None)
        if not callable(resolve_fn):
            return 0
        return int(await resolve_fn(
            session_key, choice, request_id=request_id or None,
            reason=reason,
        ) or 0)

    def _unwire_approval_ingest(self) -> None:
        """Undo :meth:`_wire_approval_ingest` (shutdown; never raises).

        Unregistering the gateway notify also releases any thread still
        blocked in the gateway wait loop (``unregister_gateway_notify``
        sets their events) so shutdown never hangs on a pending approval.
        The frame hook is cleared only when it is still ours (never yank
        a hook someone else installed after us)."""
        try:
            from observatory.gateway_session import GATEWAY_APPROVAL_SESSION_KEY
        except Exception:
            GATEWAY_APPROVAL_SESSION_KEY = "session:gateway"  # type: ignore[assignment]
        try:
            from tools.approval import unregister_gateway_notify
        except Exception:
            unregister_gateway_notify = None  # type: ignore[assignment]
        if unregister_gateway_notify is not None:
            try:
                unregister_gateway_notify(GATEWAY_APPROVAL_SESSION_KEY)
            except Exception:
                pass
        try:
            from tools.omp_rpc_transport import set_approval_frame_hook
        except Exception:
            set_approval_frame_hook = None  # type: ignore[assignment]
        if set_approval_frame_hook is not None:
            try:
                import tools.omp_rpc_transport as _rpc_transport
                current = getattr(_rpc_transport, "_approval_frame_hook", None)
                # Bound methods compare unequal across attribute reads —
                # compare the underlying instance + function instead.
                if (getattr(current, "__self__", None) is self
                        and getattr(current, "__func__", None)
                        is type(self)._omp_approval_frame_router):
                    set_approval_frame_hook(None)
            except Exception:
                pass

    def _omp_approval_frame_router(
        self, request_id: str, method: str, title: str, options: tuple
    ) -> None:
        """Global omp approval-frame hook → bridge (never raises, never
        affects the guard decision — purely observational).

        Attributes the frame to the single live omp node when unambiguous;
        drops it (debug log) when zero or several omp nodes are live — a
        prompt in the wrong room is worse than none. The submission
        carries the responder thread's ambient approval key (the queue the
        guard blocks on), read at fire time, so room /approve resolves the
        exact waiter through the bridge resolver above."""
        bridge = self.approvals
        state = self.state
        if bridge is None or state is None:
            return
        try:
            from observatory.approvals import rpc_frame_callback
            from tools.approval import get_current_session_key
        except Exception:
            return
        try:
            candidates = [
                row for row in state.get_live()
                if str(row.get("engine") or "") == "omp"
            ]
        except Exception:
            return
        if len(candidates) != 1:
            log.debug(
                "omp approval frame %r: %d live omp nodes — skipped (ambiguous)",
                request_id, len(candidates),
            )
            return
        node_id = str(candidates[0].get("node_id") or "")
        if not node_id:
            return
        try:
            session_key = get_current_session_key()
        except Exception:
            session_key = ""
        try:
            rpc_frame_callback(bridge, node_id, session_key)(
                request_id, method, title, options)
        except Exception:
            log.exception("omp approval frame submit failed")

    def _attach_omp_feeds(self) -> None:
        """One OmpFeed per live spawned omp RPC child (registry handles).

        Gateway-origin omp children are NOT covered here: they live in the
        gateway process's ``tools.omp_delegation._live_children`` table,
        invisible to this separate sidecar process (a same-process import
        of that table always fails here — it was removed, not fixed).
        Their feed arrives cross-process as ``child_lifecycle`` /
        ``child_event`` datagrams on the gateway-progress socket (pushed
        by the gateway's own child-feed watcher, ingested by
        :meth:`_handle_gateway_live_datagram`), which create and render
        the child nodes. Grandchildren render into their own rooms; the
        subagent→node map lives per feed.
        """
        from observatory.omp_feed import OmpFeed

        assert self.registry is not None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # type: ignore[assignment]
        else:
            loop = True  # type: ignore[assignment]
        for handle in self.registry.handles():
            rpc = getattr(handle, "rpc", None)
            if rpc is None or handle.node_id in self.omp_feeds:
                continue
            feed = OmpFeed(rpc)
            self.omp_feeds[handle.node_id] = feed
            if loop is None:
                continue
            self._loops.append(
                asyncio.create_task(
                    self._run_omp_feed(handle.node_id, feed),
                    name=f"observatory-omp-feed-{handle.node_id}",
                )
            )

    def _start_gateway_live_listener(self) -> None:
        """Bind the live-ingest unix datagram socket (best-effort).

        Path: ``$MERCURY_HOME/observatory/gateway-progress.sock``. The
        gateway pushes turn-progress ``{node_id, seq, event}`` datagrams
        plus gateway-child ``child_lifecycle``/``child_event`` frames.
        Bind failure disables live rendering only (warning) — the batched
        ``_replay_gateway_events`` path still works.
        """
        if self._gateway_live_sock is not None:
            return
        try:
            sock_path = gateway_progress_sock_path(self.mercury_home)
        except Exception:
            log.exception("gateway live ingest disabled: bad socket path")
            return
        import socket as _socket

        try:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            log.warning("gateway live ingest disabled: cannot mkdir %s", sock_path.parent)
            return
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        try:
            if sock_path.exists():
                try:
                    sock_path.unlink()
                except OSError:
                    pass
            sock.bind(str(sock_path))
        except OSError as exc:
            log.warning(
                "gateway live ingest disabled: bind %s failed: %s "
                "(batched replay still works)", sock_path, exc)
            try:
                sock.close()
            except OSError:
                pass
            return
        try:
            sock.setblocking(False)
        except OSError:
            pass
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("gateway live ingest disabled: no running loop")
            try:
                sock.close()
            except OSError:
                pass
            return
        self._gateway_live_sock = sock
        self._gateway_live_enabled = True

        def _on_readable() -> None:
            try:
                while True:
                    try:
                        data, _addr = sock.recvfrom(GATEWAY_LIVE_DATAGRAM_MAX)
                    except BlockingIOError:
                        break
                    except OSError as exc:
                        log.debug("gateway live recv failed: %s", exc)
                        break
                    if not data:
                        continue
                    try:
                        loop.create_task(
                            self._handle_gateway_live_datagram(data),
                            name="observatory-gateway-live")
                    except RuntimeError:
                        break
            except Exception:
                log.exception("gateway live readable handler failed")

        try:
            loop.add_reader(sock.fileno(), _on_readable)
        except Exception as exc:
            log.warning(
                "gateway live ingest disabled: add_reader failed: %s "
                "(batched replay still works)", exc)
            try:
                sock.close()
            except OSError:
                pass
            self._gateway_live_sock = None
            self._gateway_live_enabled = False
            return
        log.info("gateway live ingest listening on %s", sock_path)

    def _stop_gateway_live_listener(self) -> None:
        """Best-effort teardown of the live-ingest socket (never raises)."""
        sock = self._gateway_live_sock
        self._gateway_live_sock = None
        self._gateway_live_enabled = False
        if sock is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None  # type: ignore[assignment]
        if loop is not None:
            try:
                loop.remove_reader(sock.fileno())
            except Exception:
                pass
        try:
            sock.close()
        except OSError:
            pass
        try:
            sock_path = gateway_progress_sock_path(self.mercury_home)
            if sock_path.exists():
                try:
                    sock_path.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    async def _handle_gateway_live_datagram(self, data: bytes) -> None:
        """Parse one live-ingest datagram → live render (never raises).

        Four shapes share the socket: legacy turn-progress
        ``{node_id, seq, event}`` (no ``kind``), the gateway-child
        feed ``{"kind": "child_lifecycle" | "child_event", "node_id",
        ...}``, and guard approvals ``{"kind": "approval_prompt", ...}``
        (gateway-turn prompts → room mirror). Unknown or malformed
        payloads are skipped.
        """
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            log.debug("gateway live datagram: bad JSON, skipped", exc_info=True)
            return
        if not isinstance(payload, dict):
            return
        kind = str(payload.get("kind") or "")
        try:
            if kind in ("", "turn_progress"):
                await self._handle_turn_progress_datagram(payload)
            elif kind == "child_lifecycle":
                await self._handle_child_lifecycle_datagram(payload)
            elif kind == "child_event":
                await self._handle_child_feed_datagram(payload)
            elif kind == "approval_prompt":
                await self._handle_approval_prompt_datagram(payload)
            else:
                log.debug("gateway live datagram: unknown kind %r, skipped", kind)
        except Exception:
            log.debug("gateway live datagram handling failed", exc_info=True)

    async def _handle_approval_prompt_datagram(self, payload: dict) -> None:
        """Gateway-process guard prompt → bridge room mirror (never raises).

        The bridge is the approval source of truth (its pendings are what
        room /approve|/deny resolves); the control router's own approval
        table is deliberately NOT fed — feeding both would double-post
        every decision (see ``_wire_approval_ingest`` / ``_route_inbound``).
        """
        try:
            from observatory.gateway_session import GATEWAY_APPROVAL_SESSION_KEY
        except Exception:
            GATEWAY_APPROVAL_SESSION_KEY = "session:gateway"  # type: ignore[assignment]
        try:
            bridge = self.approvals
            if bridge is None:
                return
            node_id = str(payload.get("node_id") or "")
            request_id = str(payload.get("request_id") or "")
            command = str(payload.get("command") or "")
            context = str(payload.get("description") or "")
            session_key = str(
                payload.get("session_key") or GATEWAY_APPROVAL_SESSION_KEY)
            if not node_id or not request_id:
                log.debug(
                    "approval prompt datagram: missing node/request, skipped")
                return
            await bridge.submit(
                node_id, request_id, backend="gateway",
                session_key=session_key,
                command=command or "(approval)",
                context=context,
            )
        except Exception:
            log.debug("approval prompt datagram handling failed", exc_info=True)

    async def _handle_turn_progress_datagram(self, payload: dict) -> None:
        """One ``{node_id, seq, event}`` turn frame → live render."""
        node_id = str(payload.get("node_id") or payload.get("node") or "")
        event = payload.get("event")
        if event is None:
            event = {
                k: v for k, v in payload.items()
                if k not in ("node_id", "node", "seq", "kind", "internal")
            }
        if not node_id or not isinstance(event, dict) or not event:
            log.debug("gateway live datagram: missing node_id/event, skipped: %r", payload)
            return
        seq = _coerce_live_seq(payload.get("seq"))
        ev_seq = _coerce_live_seq(event.get("seq"))
        seqs = {s for s in (seq, ev_seq) if s is not None}
        primary = seq if seq is not None else ev_seq
        # BUG2: internal follow-up turns collapse progress to ONE liveness
        # notice, not per-event messages. Record seqs for the replay dedupe
        # but skip the per-event render here; replay logs without room sends.
        is_internal = bool(payload.get("internal") or event.get("internal") or self._gateway_internal_turns.get(node_id))
        if is_internal:
            if seqs:
                self._gateway_live_seqs.setdefault(node_id, set()).update(seqs)
            log.debug("gateway live internal event collapsed (node %s seq %s)", node_id, primary)
            return
        try:
            await self._render_gateway_live_event(node_id, primary, event)
        except Exception:
            log.debug("gateway live render failed (node %s)", node_id, exc_info=True)
            return
        if seqs:
            self._gateway_live_seqs.setdefault(node_id, set()).update(seqs)

    async def _cot_status_post(self, node_id: str, seq: int = 0) -> str | None:
        """Post ONE Telegram-shaped status message for a gateway turn."""
        try:
            if self.renderer is None or self.state is None:
                return None
            body = cot_status_text(seq)
            try:
                voice = self.state.get(node_id)["mxid"]
            except Exception:
                voice = self.gateway_mxid
            records = await self.renderer.executor.execute(
                [SendMessage(node_id, voice, body)]
            )
            event_id = None
            try:
                if records and isinstance(records[0], dict) and records[0].get("op") == "send":
                    event_id = records[0].get("event_id")
            except Exception:
                event_id = None
            if event_id:
                self._cot_status_event[node_id] = str(event_id)
                return str(event_id)
            return None
        except Exception:
            log.debug("cot status post failed (node %s)", node_id, exc_info=True)
            return None

    async def _cot_status_edit(self, node_id: str, seq: int = 0) -> bool:
        """Edit the turn's status in place (same event id, new face/verb)."""
        try:
            if self.renderer is None or self.state is None:
                return False
            event_id = self._cot_status_event.get(node_id)
            if not event_id:
                return bool(await self._cot_status_post(node_id, seq))
            body = cot_status_text(seq)
            try:
                voice = self.state.get(node_id)["mxid"]
            except Exception:
                voice = self.gateway_mxid
            await self.renderer.executor.execute(
                [EditMessage(node_id, voice, str(event_id), body)]
            )
            return True
        except Exception:
            log.debug("cot status edit failed (node %s)", node_id, exc_info=True)
            return False

    async def _cot_status_seal(self, node_id: str, reply: str) -> bool:
        """Seal the status into the final reply when short. True when sealed."""
        try:
            if self.renderer is None or self.state is None:
                return False
            event_id = self._cot_status_event.get(node_id)
            if not event_id:
                return False
            if not cot_status_seal_short(reply):
                return False
            try:
                voice = self.state.get(node_id)["mxid"]
            except Exception:
                voice = self.gateway_mxid
            await self.renderer.executor.execute(
                [EditMessage(node_id, voice, str(event_id), reply)]
            )
            try:
                self._cot_status_event.pop(node_id, None)
            except Exception:
                pass
            return True
        except Exception:
            log.debug("cot status seal failed (node %s)", node_id, exc_info=True)
            return False

    async def _render_gateway_live_event(
        self, node_id: str, seq: int | None, event: dict
    ) -> None:
        """Render one live event (same gates as batched replay)."""
        assert self.renderer is not None
        if not isinstance(event, dict):
            return
        # BUG2: internal events never render per-event live (ONE liveness
        # notice covers the whole turn — posted by _deliver_gateway_prompt).
        if event.get("internal") or self._gateway_internal_turns.get(node_id):
            return
        etype = str(event.get("type") or "")
        if etype in ("tool_call", "tool"):
            tool = str(event.get("tool") or "")
            if not tool:
                return
            await self.renderer.render_tool_call(
                node_id, tool, _live_event_args_text(event.get("args")))
        elif etype in ("thinking", "thought", "reasoning"):
            text_val = event.get("text")
            if not isinstance(text_val, str) or not text_val.strip():
                return
            router = self.control_router
            cot_on = bool(router.cot_enabled(node_id)) if router is not None else False
            if cot_on:
                await self.renderer.render_thinking(node_id, text_val)
                return
            try:
                s = seq if isinstance(seq, int) else 0
            except Exception:
                s = 0
            try:
                await self._cot_status_edit(node_id, s)
            except Exception:
                log.debug("cot status edit failed (node %s)", node_id, exc_info=True)
            return
        else:
            return

    async def _ensure_datagram_child_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        goal: str | None = None,
        delegation_id: str | None = None,
        task_index: int | None = None,
        parent_session: str | None = None,
    ) -> bool:
        """Create the datagram child node when absent; True when created.

        Discovery may create the same node first (poll/hook race) — the
        ``state.get`` guard makes creation exactly-once; late frames for
        an unknown node build a minimal stub so no frame is ever dropped.
        """
        assert self.state is not None and self.renderer is not None
        try:
            self.state.get(node_id)
            return False
        except StateError:
            pass
        node_name = (name or "").strip() or node_id
        try:
            parent = self._resolve_parent_node(parent_session or "")
        except Exception:
            parent = GATEWAY_NODE_ID
        slug = assign_slug(node_name, self.state)
        mxid = virtual_mxid(slug, server_name=self.server_name)
        extra: dict[str, Any] = {"engine_child": True}
        if isinstance(delegation_id, str) and delegation_id:
            extra["delegation_id"] = delegation_id
        if isinstance(task_index, int):
            extra["task_index"] = task_index
        if isinstance(goal, str) and goal:
            extra["goal"] = goal
        try:
            self.state.add_node(
                node_id,
                engine="omp",
                name=node_name,
                slug=slug,
                mxid=mxid,
                session_ref=f"omp-child:{node_id}",
                parent_node_id=parent,
                extra=extra,
            )
        except Exception:
            log.debug("datagram child node %s already created (race)", node_id)
            return False
        if self.client is not None:
            localpart = mxid.lstrip("@").split(":", 1)[0]
            try:
                await self.client.register_virtual_user(localpart)
            except Exception as exc:  # noqa: BLE001 — ghost may auto-provision
                log.info("register %s: %s", localpart, exc)
        await self.renderer.apply_plan(
            self.renderer.build_plan(host=socket.gethostname())
        )
        return True

    async def _handle_child_lifecycle_datagram(self, payload: dict) -> None:
        """Gateway-child start/stop → node create + provision + render."""
        node_id = str(payload.get("node_id") or "")
        lifecycle = str(payload.get("lifecycle") or "")
        if not node_id or lifecycle not in ("start", "stop"):
            log.debug("child lifecycle datagram: bad shape, skipped: %r", payload)
            return
        if self.state is None or self.renderer is None:
            return
        if lifecycle == "start":
            task_index = payload.get("task_index")
            await self._ensure_datagram_child_node(
                node_id,
                name=(str(payload.get("name")) if payload.get("name") is not None else None),
                goal=(str(payload.get("goal")) if payload.get("goal") is not None else None),
                delegation_id=(
                    str(payload.get("delegation_id"))
                    if payload.get("delegation_id") is not None else None
                ),
                task_index=(int(task_index) if isinstance(task_index, int) else None),
                parent_session=(
                    str(payload.get("parent_session"))
                    if payload.get("parent_session") else None
                ),
            )
            try:
                row = self.state.get(node_id)
            except StateError:
                return
            if row.get("room_id"):
                await self.renderer.render_lifecycle(node_id)
            else:
                log.info(
                    "gateway child %s observed without a planned room — "
                    "lifecycle render skipped", node_id,
                )
            return
        try:
            row = self.state.get(node_id)
        except StateError:
            return  # stop for a node we never saw — nothing to render
        if row["status"] != "live":
            return
        status = str(payload.get("status") or "unknown")
        summary = payload.get("summary")
        summary_text = str(summary) if summary is not None else None
        await self.renderer.render_death(node_id, status=status, summary=summary_text)
        try:
            await self._maybe_post_delegate_followup(
                node_id, str(row.get("parent_node_id") or ""),
                str(row.get("name") or node_id),
                status=status, summary=summary,
            )
        except Exception:
            log.debug("post-delegate followup failed for %s", node_id, exc_info=True)

    async def _handle_child_feed_datagram(self, payload: dict) -> None:
        """Forwarded child feed frame → grandchild-mapped render."""
        node_id = str(payload.get("node_id") or "")
        feed = payload.get("feed")
        if not node_id or not isinstance(feed, dict) or not feed:
            log.debug("child feed datagram: bad shape, skipped: %r", payload)
            return
        if self.state is None or self.renderer is None:
            return
        await self._ensure_datagram_child_node(node_id)
        ftype = str(feed.get("feed") or "")
        boxes = self._datagram_grandchildren.setdefault(node_id, {})
        try:
            if ftype == "node":
                subagent_id = str(feed.get("subagent_id") or "")
                if not subagent_id:
                    return
                if str(feed.get("kind") or "") == "death":
                    target = boxes.get(subagent_id) or f"{node_id}/gc:{subagent_id}"
                    try:
                        if self.state.get(target)["status"] == "live":
                            await self.renderer.render_death(
                                target, status=str(feed.get("status") or "completed"))
                    except StateError:
                        pass
                    return
                from types import SimpleNamespace

                adapted = SimpleNamespace(
                    kind="add",
                    subagent_id=subagent_id,
                    parent_tool_call_id=feed.get("parent_tool_call_id"),
                    status="running",
                    agent=feed.get("agent") or feed.get("task") or subagent_id,
                    task=feed.get("task"),
                    session_file=feed.get("session_file"),
                )
                boxes[subagent_id] = await self._render_grandchild(node_id, adapted)
            elif ftype == "tool":
                subagent_id = str(feed.get("subagent_id") or "")
                target = boxes.get(subagent_id) if subagent_id else None
                if not target and subagent_id:
                    # FOLLOW-UP B: feed race/restart — boxes map lost but the
                    # node row survives. Resolve via the deterministic id and
                    # re-adopt instead of dropping (dropped frames = empty rooms).
                    target = f"{node_id}/gc:{subagent_id}"
                    try:
                        self.state.get(target)
                        boxes[subagent_id] = target
                    except StateError:
                        target = None
                if not target:
                    return
                try:
                    if self.state.get(target)["status"] != "live":
                        return
                except StateError:
                    return
                tool = str(feed.get("tool") or "")
                if not tool:
                    return
                await self.renderer.render_tool_call(
                    target, tool, _live_event_args_text(feed.get("args")))
            elif ftype == "thought":
                subagent_id = str(feed.get("subagent_id") or "")
                target = boxes.get(subagent_id) if subagent_id else None
                if not target and subagent_id:
                    target = f"{node_id}/gc:{subagent_id}"
                    try:
                        self.state.get(target)
                        boxes[subagent_id] = target
                    except StateError:
                        target = None
                text_val = feed.get("text")
                if not target or not isinstance(text_val, str) or not text_val.strip():
                    return
                try:
                    if self.state.get(target)["status"] != "live":
                        return
                except StateError:
                    return
                router = self.control_router
                if router is not None and not router.cot_enabled(target):
                    return
                await self.renderer.render_thinking(target, text_val)
            else:
                log.debug("child feed datagram: unknown feed %r, skipped", ftype)
        except Exception:
            log.debug("child feed render failed (node %s)", node_id, exc_info=True)

    # --- discovery → state → renderer (§7 + §5) ---------------------------------

    def _start_discovery(self) -> None:
        from observatory.platform_hook import build_discovery

        self.discovery = build_discovery(self.mercury_home, poll_interval=self.poll_interval)
        self._discovery_task = asyncio.create_task(
            self._run_discovery(), name="observatory-discovery"
        )

    async def _run_discovery(self) -> None:
        assert self.discovery is not None
        await self.discovery.start()
        try:
            async for event in self.discovery.stream():
                try:
                    await self._apply_discovery_event(event)
                except Exception:  # noqa: BLE001 — one bad event must not kill the stream
                    log.exception("discovery event failed: %r", event)
        finally:
            await self.discovery.stop()

    async def _apply_discovery_event(self, event: Any) -> None:
        """discovery.NodeEvent → state node + renderer render.

        Parent resolution: ``parent_session`` maps to the live node whose
        ``session_ref``/``extra.session_id`` carries it (the spawning
        orchestrator); unmapped sessions fall back to the gateway agent.

        Render: the landed tree module plans EVERY live node — roots as
        orchestrator subspaces of the root space, gateway-origin
        delegation children as subspaces nested under the gateway agent's
        own subspace (gw-space parity) — so every add provisions a space
        + room before its lifecycle message renders. The room-existence
        guard stays as defense (a plan that cannot place a node logs and
        skips the lifecycle render rather than crashing)."""
        from observatory.discovery import NodeEvent

        assert isinstance(event, NodeEvent) and self.state is not None and self.renderer is not None
        node_id = f"{event.delegation_id}/{event.task_index}"
        if event.kind == "add":
            try:
                self.state.get(node_id)
                return  # already known (poll/hook dedupe at state level too)
            except StateError:
                pass
            parent = self._resolve_parent_node(event.parent_session)
            slug = assign_slug(event.name, self.state)
            mxid = virtual_mxid(slug, server_name=self.server_name)
            self.state.add_node(
                node_id,
                engine="hermes",
                name=event.name,
                slug=slug,
                mxid=mxid,
                session_ref=f"delegation:{event.delegation_id}",
                parent_node_id=parent,
                extra={"delegation_id": event.delegation_id, "task_index": event.task_index},
            )
            if self.client is not None:
                localpart = mxid.lstrip("@").split(":", 1)[0]
                try:
                    await self.client.register_virtual_user(localpart)
                except Exception as exc:  # noqa: BLE001 — ghost may auto-provision
                    log.info("register %s: %s", localpart, exc)
            await self.renderer.apply_plan(
                self.renderer.build_plan(host=socket.gethostname())
            )
            row = self.state.get(node_id)
            if row.get("room_id"):
                await self.renderer.render_lifecycle(node_id)
            else:
                log.info(
                    "delegation %s observed without a planned room — "
                    "lifecycle render skipped (plan covers roots, "
                    "gateway-origin children and their subtrees)", node_id,
                )
        elif event.kind == "death":
            try:
                row = self.state.get(node_id)
            except StateError:
                return  # death for a node we never saw — nothing to render
            parent_id = str(row.get("parent_node_id") or "")
            child_name = str(row.get("name") or node_id)
            if row["status"] == "live":
                await self.renderer.render_death(
                    node_id, status=event.status, summary=event.summary
                )
                await self._maybe_post_delegate_followup(
                    node_id, parent_id, child_name,
                    status=str(event.status or ""),
                    summary=event.summary,
                )

    def _resolve_parent_node(self, parent_session: str) -> str:
        """parent session id → owning node; gateway fallback."""
        assert self.state is not None
        if parent_session:
            for row in self.state.get_live():
                extra = row.get("extra") or {}
                if row["session_ref"] == parent_session or extra.get("session_id") == parent_session:
                    return row["node_id"]
        return GATEWAY_NODE_ID

    def _start_loops(self) -> None:
        self._loops.append(
            asyncio.create_task(self._loop("approvals-expiry", APPROVALS_EXPIRY_INTERVAL,
                                           self._approvals_tick), name="observatory-approvals")
        )
        self._loops.append(
            asyncio.create_task(self._loop("cron-poll", CRON_POLL_INTERVAL,
                                           self._cron_tick), name="observatory-cron")
        )
        self._loops.append(
            asyncio.create_task(self._loop("manual-poll", MANUAL_POLL_INTERVAL,
                                           self._manual_tick), name="observatory-manual")
        )
        self._loops.append(
            asyncio.create_task(self._loop("admin-token-heal", ADMIN_TOKEN_HEAL_INTERVAL,
                                           self._admin_token_tick), name="observatory-admin-heal")
        )

    async def _loop(self, name: str, interval: float, tick) -> None:
        while not self._stop_event.is_set():
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — loop ticks are independent
                log.exception("%s tick failed (continuing)", name)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _approvals_tick(self) -> None:
        if self.approvals is not None:
            await self.approvals.check_expiry()
        await self._refresh_power_levels()

    async def _refresh_power_levels(self) -> None:
        """Warm/refresh the D7 snapshot from the homeserver (authoritative).
        Failures keep the previous snapshot (stale beats closed)."""
        from observatory.control import RoomPowerLevels

        if self.client is None or self.state is None:
            return
        rooms: set[str] = set()
        for row in self.state.get_live():
            for rid in (row.get("room_id"), row.get("space_id")):
                if rid:
                    rooms.add(rid)
        directives = self._directives_room_id()
        if directives:
            rooms.add(directives)
        for room_id in rooms:
            try:
                raw = await self.client.get_power_levels(room_id, sender=self.gateway_mxid)
            except Exception:  # noqa: BLE001 — keep prior snapshot on failure
                continue
            if isinstance(raw, dict):
                self._pl_cache[room_id] = RoomPowerLevels(
                    users=dict(raw.get("users") or {}),
                    events_default=int(raw.get("events_default") or 0),
                    users_default=int(raw.get("users_default") or 0),
                )

    def _apply_pl_event(self, event: dict) -> None:
        """Keep the snapshot fresh from inbound transactions (member/PL
        changes arrive as events — no extra fetch needed)."""
        etype = str(event.get("type") or "")
        room_id = str(event.get("room_id") or "")
        if not room_id:
            return
        if etype == "m.room.power_levels":
            from observatory.control import RoomPowerLevels

            content = event.get("content") or {}
            if isinstance(content, dict):
                self._pl_cache[room_id] = RoomPowerLevels(
                    users=dict(content.get("users") or {}),
                    events_default=int(content.get("events_default") or 0),
                    users_default=int(content.get("users_default") or 0),
                )
        elif etype in ("m.room.member", "m.room.join_rules") and room_id in self._pl_cache:
            self._pl_cache.pop(room_id, None)  # membership moved — refetch

    async def _cron_tick(self) -> None:
        if self.cron_rooms is not None:
            await self.cron_rooms.apply_registry()
            await self.cron_rooms.render_poll()

    async def _manual_tick(self) -> None:
        if self.manual_runs is not None:
            await self.manual_runs.render_poll()

    async def _run_omp_feed(self, node_id: str, feed: Any) -> None:
        """Consume one omp child's typed events: grandchildren lifecycle,
        tool calls, thinking (§5)."""
        from observatory.omp_feed import NodeEvent, ThoughtEvent, ToolEvent

        await feed.start()
        grandchild_nodes: dict[str, str] = {}
        try:
            async for event in feed.events():
                try:
                    if isinstance(event, NodeEvent):
                        grandchild_nodes[event.subagent_id] = await self._render_grandchild(
                            node_id, event
                        )
                    elif isinstance(event, ToolEvent):
                        target = grandchild_nodes.get(event.subagent_id)
                        if target:
                            await self.renderer.render_tool_call(target, event.tool, event.args)
                    elif isinstance(event, ThoughtEvent):
                        target = grandchild_nodes.get(event.subagent_id)
                        if target and self.control_router is not None \
                                and self.control_router.cot_enabled(target):
                            await self.renderer.render_thinking(target, event.text)
                except Exception:  # noqa: BLE001 — one bad frame must not kill the feed
                    log.exception("omp feed event failed: %r", event)
        finally:
            await feed.stop()

    async def _render_grandchild(self, parent_node_id: str, event: Any) -> str:
        """omp grandchild add/death → its own node under the child."""
        assert self.state is not None and self.renderer is not None
        node_id = f"{parent_node_id}/gc:{event.subagent_id}"
        if event.kind == "death":
            try:
                if self.state.get(node_id)["status"] == "live":
                    await self.renderer.render_death(node_id, status=event.status)
            except StateError:
                pass
            return node_id
        try:
            self.state.get(node_id)
            return node_id  # already known
        except StateError:
            pass
        name = event.agent or event.task or event.subagent_id
        slug = assign_slug(name, self.state)
        self.state.add_node(
            node_id,
            engine="omp",
            name=name,
            slug=slug,
            mxid=virtual_mxid(slug, server_name=self.server_name),
            session_ref=f"omp-subagent:{event.subagent_id}",
            parent_node_id=parent_node_id,
            extra={"subagent_id": event.subagent_id},
        )
        if self.client is not None:
            localpart = virtual_mxid(slug, server_name=self.server_name).lstrip("@").split(":", 1)[0]
            try:
                await self.client.register_virtual_user(localpart)
            except Exception as exc:  # noqa: BLE001
                log.info("register %s: %s", localpart, exc)
        await self.renderer.apply_plan(self.renderer.build_plan(host=socket.gethostname()))
        await self.renderer.render_lifecycle(node_id)
        return node_id

    # --- directives room (§6) ------------------------------------------------------


    def _directives_members(self) -> list[str]:
        assert self.state is not None
        return [row["mxid"] for row in self.state.get_live()]

    async def _sync_directives_membership(self) -> None:
        """D12: gateway agent + every live 0-agent, auto-maintained."""
        if self.directives is None:
            return
        current = await self._room_members(self._directives_room_id())
        await self.directives.sync_membership(current)

    async def _room_members(self, room_id: str) -> list[str]:
        if not room_id or self.client is None:
            return []
        from urllib.parse import quote

        out = await self.client.client_api(
            "GET", f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/members",
            sender=self.gateway_mxid,
        )
        chunk = (out or {}).get("chunk", []) if isinstance(out, dict) else []
        return [
            e["state_key"] for e in chunk
            if isinstance(e, dict) and (e.get("content") or {}).get("membership") in ("join", "invite")
        ]

    # --- inbound transactions ---------------------------------------------------
    async def _on_crypto(self, txn: dict[str, Any]) -> None:
        """Crypto side-channel consumer: route the transaction's to-device
        messages, device-list deltas and OTK counts into the per-user
        machines (Olm pre-keys, room keys, key requests). Runs BEFORE the
        room events of the same transaction so inbound room keys land
        before decrypt needs them. E2EE off → no-op."""
        if self.e2ee is None:
            return
        try:
            routed = await self.e2ee.handle_as_transaction(txn)
            if any(routed.values()):
                log.info("e2ee crypto routed %s", routed)
        except Exception:  # noqa: BLE001 — the intake survives handler bugs
            log.exception("e2ee crypto routing failed")

    async def _on_transaction(self, txn_id: str, events: list[dict[str, Any]]) -> None:
        """Intake consumer: decrypt (E2EE) → directives delivery / control
        routing / approval resolution → notices posted back."""
        directives_room = self._directives_room_id()
        for event in events:
            if self.e2ee is not None and str(event.get("type")) == "m.room.encrypted":
                decrypted = await self.e2ee.decrypt_event(event)
                if decrypted is not None:
                    event = {**event, "type": "m.room.message", "content": decrypted}
                else:
                    await self._notice_decrypt_failure(event)
            self.seen_events.append(event)
            if len(self.seen_events) > 1024:  # bounded observation buffer
                del self.seen_events[:512]
            if not isinstance(event, dict):
                continue
            self._apply_pl_event(event)  # D7 snapshot stays fresh
            try:
                await self._route_inbound(txn_id, event, directives_room)
            except Exception:  # noqa: BLE001 — the intake survives handler bugs
                log.exception("inbound routing failed in txn %s", txn_id)

    def _directives_room_id(self) -> str:
        if self.state is None:
            return ""
        try:
            return self.state.get_meta("room:" + DIRECTIVES_ROOM_KEY)
        except StateError:
            return ""

    async def _notice_decrypt_failure(self, event: dict[str, Any]) -> None:
        """Surface one undecryptable event with recovery steps (defect iii).

        One notice per room per process (later failures only log) — the
        intake never dies on a notice failure. Never raises. Membership is
        checked first: the gateway ghost is only a member of rooms it was
        joined to at creation, so a notice into a room it never joined
        skips silently at debug (no 403 traceback spam).
        """
        try:
            room_id = str(event.get("room_id") or "")
            event_id = str(event.get("event_id") or "")
            if not room_id or room_id in self._decrypt_notified:
                log.warning("megolm decrypt failed for %s (already notified: %s)",
                            event_id, room_id)
                return
            self._decrypt_notified.add(room_id)
            if len(self._decrypt_notified) > 64:
                self._decrypt_notified.pop()
            if self.client is None or not self.gateway_mxid:
                return
            try:
                members = await self._room_members(room_id)
            except Exception as exc:  # noqa: BLE001 — unreadable == not-member
                log.debug("decrypt-failure notice skipped (members unreadable %s): %s",
                          room_id, exc)
                return
            if self.gateway_mxid not in (members or []):
                log.debug("decrypt-failure notice skipped (gateway not in %s)", room_id)
                return
            from observatory.e2ee import decrypt_failure_notice

            try:
                await self.client.send_message(
                    room_id, decrypt_failure_notice(event_id, room_id),
                    sender=self.gateway_mxid)
            except Exception as exc:  # noqa: BLE001 — notices never kill the intake
                log.debug("decrypt-failure notice skipped for %s: %s", room_id, exc)
                return
        except Exception as exc:  # noqa: BLE001 — notices never kill the intake
            log.debug("decrypt-failure notice skipped: %s", exc)

    async def _route_inbound(self, txn_id: str, event: dict, directives_room: str) -> None:
        # §6 directives room: owner-only mention-gated fan-out
        if directives_room and event.get("room_id") == directives_room:
            if self.directives is not None:
                outcome = await self.directives.handle_message(
                    str(event.get("sender") or ""), event.get("content") or {}
                )
                self.routing_log.append(f"directives:{len(outcome.targets) if outcome.targets else 'none'}")
            return
        # M4b approval replies (reply-to-prompt resolution)
        bridge_action: str | None = None
        if self.approvals is not None:
            bridge_action = await self.approvals.handle_event(event)
            if bridge_action is not None:
                self.routing_log.append(f"approvals:{bridge_action}")
        # M4a control routing (steer/stop/verbs/commands)
        if self.control_router is not None:
            if isinstance(event, dict) and event.get("type") == "m.room.message":
                # Cold-start heal: rooms converged after boot (freshly
                # spawned children) have no PL snapshot yet, and the
                # router fails CLOSED — one live fetch per unknown room
                # so the first message routes instead of bouncing with
                # "still starting up". Failure keeps fail-closed.
                try:
                    await self._ensure_pl_for_room(str(event.get("room_id") or ""))
                except Exception:  # noqa: BLE001 — warm failure keeps closed
                    log.debug("pl warm failed (keeping fail-closed)", exc_info=True)
            outcomes = await self.control_router.handle_transaction(txn_id, [event])
            bridge_claimed = bridge_action is not None and bridge_action.split(":", 1)[0] in (
                "resolved", "denied", "late", "error")
            for outcome in outcomes:
                self.routing_log.append(outcome.disposition)
                if bridge_claimed and outcome.disposition == "notice:no-approval-pending":
                    # The bridge owns approval resolution (its pendings are
                    # fed by gateway_notify / the frame hook / gateway
                    # approval_prompt datagrams; the router's own approval
                    # table is never fed — see _wire_approval_ingest). The
                    # bridge just answered the /approve|/deny itself (✔/🚫
                    # decision, denial, or late/error notice); the router's
                    # stale "no pending approval" notice would contradict
                    # it in the same room — drop the notice, keep the log.
                    continue
                if self._is_gateway_prompt(outcome):
                    await self._handle_gateway_prompt_outcome(outcome)
                    continue
                if await self._handle_gateway_abort_outcome(outcome):
                    for notice in outcome.notices:
                        if "stop requested" not in str(getattr(notice, "body", "")):
                            await self._post_notice(notice)
                    continue
                if await self._handle_observatory_verb_outcome(outcome):
                    continue
                for notice in outcome.notices:
                    await self._post_notice(notice)
                await self._execute_child_actions(outcome)

    def _observatory_verb(self, text: str) -> str:
        """Lowercase /verb or !verb head of an EngineCommand text, else ''."""
        try:
            stripped = (text or "").strip()
            if not stripped or stripped[0] not in ("/", "!"):
                return ""
            head = stripped[1:].split(None, 1)
            if not head:
                return ""
            verb = head[0].lower()
            if "@" in verb:
                verb = verb.split("@", 1)[0]
            return verb
        except Exception:
            return ""

    async def _handle_observatory_verb_outcome(self, outcome: Any) -> bool:
        """Spawned-room /spawn+/spawnomp+/exit via the generic gateway slash
        dispatch (same transport + inject verb as gateway prompts — no second
        Matrix path). Only these observatory verbs route here; every other
        non-gateway InjectText (child steers, session-scoped commands)
        delivers via _execute_child_actions. True when handled."""
        try:
            actions = list(getattr(outcome, "actions", ()) or ())
        except Exception:
            return False
        if len(actions) != 1 or not isinstance(actions[0], InjectText):
            return False
        if str(getattr(actions[0], "kind", "") or "") != "command":
            return False
        if self._observatory_verb(str(getattr(actions[0], "text", "") or "")) not in (
            "spawn", "spawnomp", "exit",
        ):
            return False
        for notice in (getattr(outcome, "notices", ()) or ()):
            try:
                await self._post_notice(notice)
            except Exception:
                log.debug("observatory verb notice failed", exc_info=True)
        action = actions[0]
        try:
            task = asyncio.create_task(
                self._deliver_gateway_prompt(
                    str(action.node_id), str(action.text),
                    kind=str(getattr(action, "kind", None) or "command"),
                ),
                name=f"observatory-verb-prompt-{action.node_id}",
            )
        except RuntimeError:
            return True
        self._gateway_tasks.add(task)
        task.add_done_callback(self._gateway_tasks.discard)
        try:
            self.routing_log.append(f"observatory-verb:{self._observatory_verb(str(action.text))}")
        except Exception:
            pass
        return True

    async def _ensure_pl_for_room(self, room_id: str) -> None:
        """One live PL fetch for a room missing from the D7 snapshot.

        Rooms converged after boot (freshly spawned children) have no
        snapshot entry yet, and the router fails CLOSED — without this
        the first message in a new room always bounces with "still
        starting up". A failed fetch keeps fail-closed (never guesses).
        """
        if not room_id or room_id in self._pl_cache:
            return
        client = self.client
        if client is None:
            return
        try:
            raw = await client.get_power_levels(room_id, sender=self.gateway_mxid)
        except Exception:  # noqa: BLE001 — keep fail-closed
            return
        if not isinstance(raw, dict):
            return
        from observatory.control import RoomPowerLevels

        self._pl_cache[room_id] = RoomPowerLevels(
            users=dict(raw.get("users") or {}),
            events_default=int(raw.get("events_default") or 0),
            users_default=int(raw.get("users_default") or 0),
        )

    def _child_handle(self, node_id: str) -> Any | None:
        """Registry handle for a child node, resuming on demand.

        The spawn path registers into the gateway-thread boot registry
        (``platform_hook.LAST_BOOT``), which the daemon adopts at boot —
        but a child spawned after boot, or a handle lost to a restart
        without respawn, is visible in state.db with no live handle.
        That single-node resume mirrors ``respawn_pass`` (never raises:
        None means the room hears CHILD_UNAVAILABLE).
        """
        registry = self.registry
        if registry is not None:
            try:
                handle = registry.get(node_id)
            except Exception:
                handle = None
            if handle is not None:
                return handle
        if self.state is None:
            return None
        try:
            row = self.state.get(node_id)
        except StateError:
            return None
        if row.get("status") != "live":
            return None
        engine = str(row.get("engine") or "")
        try:
            if engine == "hermes":
                from observatory.respawn import resume_hermes_orchestrator
                from observatory.spawn import OrchestratorHandle

                agent = resume_hermes_orchestrator(row, mercury_home=self.mercury_home)
                handle = OrchestratorHandle(
                    node_id=node_id,
                    engine="hermes",
                    name=str(row.get("name") or node_id),
                    session_ref=str(row.get("session_ref") or ""),
                    model=(row.get("extra") or {}).get("model"),
                    agent=agent,
                )
            elif engine == "omp":
                from observatory.respawn import restart_omp_orchestrator
                from observatory.spawn import OrchestratorHandle

                child = restart_omp_orchestrator(row, mercury_home=self.mercury_home)
                handle = OrchestratorHandle(
                    node_id=node_id,
                    engine="omp",
                    name=str(row.get("name") or node_id),
                    session_ref=str(row.get("session_ref") or ""),
                    model=(row.get("extra") or {}).get("model"),
                    rpc=child,
                )
            else:
                return None
        except Exception:
            log.exception("child resume failed on demand (node %s)", node_id)
            return None
        if registry is not None:
            try:
                registry.register(handle)
            except Exception:
                log.debug(
                    "child handle register failed (node %s)", node_id, exc_info=True
                )
        if engine == "omp":
            self._ensure_omp_feed(node_id, handle)
        return handle

    def _ensure_omp_feed(self, node_id: str, handle: Any) -> None:
        """Attach one OmpFeed for an omp handle missing it (spawn-time
        handles postdate the boot attach pass). Never raises."""
        try:
            if node_id in self.omp_feeds:
                return
            rpc = getattr(handle, "rpc", None)
            if rpc is None:
                return
            from observatory.omp_feed import OmpFeed

            feed = OmpFeed(rpc)
            self.omp_feeds[node_id] = feed
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._loops.append(
                loop.create_task(
                    self._run_omp_feed(node_id, feed),
                    name=f"observatory-omp-feed-{node_id}",
                )
            )
        except Exception:
            log.exception("omp feed attach failed (node %s)", node_id)

    def _child_task_done(self, task: asyncio.Task) -> None:
        """Drop a finished child-turn task; surface unhandled failures."""
        self._child_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc is not None:
            log.error("child delivery task failed: %r", exc)

    async def _execute_child_actions(self, outcome: Any) -> None:
        """Run non-gateway control actions against the daemon registry.

        Reached only for outcomes the gateway/verb handlers did not claim:
        child-room steers, session-scoped commands, child stops, subagent
        fan-out. Previously these were only logged ("pending transport")
        — the spawned orchestrator never answered. Per-action isolation:
        one bad action never blocks its siblings or the intake.
        """
        try:
            actions = list(getattr(outcome, "actions", ()) or ())
        except Exception:
            return
        for action in actions:
            try:
                await self._execute_child_action(action)
            except Exception:  # noqa: BLE001 — intake survives handler bugs
                log.exception("child action failed: %r", action)
                try:
                    self.routing_log.append(
                        f"child-action-error:{type(action).__name__}"
                    )
                except Exception:
                    pass

    async def _execute_child_action(self, action: Any) -> None:
        """Dispatch one routed action to its engine transport."""
        if isinstance(action, InjectText):
            # Hermes-side child (steer, or a session-scoped command that
            # is not a gateway-lifecycle verb). Long turn — background
            # task so the intake never blocks.
            node_id = str(action.node_id)
            try:
                task = asyncio.create_task(
                    self._run_hermes_child_turn(
                        node_id,
                        str(action.text),
                        kind=str(getattr(action, "kind", "") or "steer"),
                    ),
                    name=f"observatory-child-turn-{node_id}",
                )
            except RuntimeError:
                await self._run_hermes_child_turn(
                    node_id,
                    str(action.text),
                    kind=str(getattr(action, "kind", "") or "steer"),
                )
                return
            self._child_tasks.add(task)
            task.add_done_callback(self._child_task_done)
        elif isinstance(action, OmpPrompt):
            # Omp main idle: a new turn. Busy-marked synchronously so a
            # message arriving mid-turn routes to steer, not a second
            # turn; cleared when the turn task ends (or is cancelled).
            node_id = str(action.node_id)
            self._child_busy.add(node_id)
            try:
                task = asyncio.create_task(
                    self._run_omp_child_prompt(node_id, str(action.text)),
                    name=f"observatory-child-prompt-{node_id}",
                )
            except RuntimeError:
                await self._run_omp_child_prompt(node_id, str(action.text))
                return
            self._child_tasks.add(task)
            task.add_done_callback(self._child_task_done)
        elif isinstance(action, OmpSteer):
            await self._steer_omp_child(str(action.node_id), str(action.text))
        elif isinstance(action, OmpSubagentSteer):
            await self._steer_omp_subagent(str(action.node_id), str(action.text))
        elif isinstance(action, AbortSession):
            await self._abort_hermes_child(
                str(action.node_id),
                str(getattr(action, "reason", "") or "matrix /stop"),
            )
        elif isinstance(action, OmpAbortMain):
            await self._abort_omp_child(
                str(action.node_id),
                str(getattr(action, "reason", "") or "matrix /stop"),
            )
        elif isinstance(action, OmpSubagentAbort):
            await self._abort_omp_subagent(
                str(action.node_id),
                str(getattr(action, "reason", "") or "matrix /stop"),
            )
        elif isinstance(action, ResolveApproval):
            # The bridge owns approval resolution (the router table is
            # never fed — see _wire_approval_ingest); a ResolveApproval
            # reaching here has no queue behind it.
            log.info("control action pending transport: %r", action)
        else:
            log.info("control action pending transport: %r", action)

    @staticmethod
    def _run_hermes_child_turn_sync(
        agent: Any, node_id: str, room_id: str, text: str, kind: str
    ) -> str:
        """Worker-thread body: session-scoped slash dispatch, else a turn.

        ``kind == "command"`` first tries the gateway slash dispatch with
        the child's observability scope (D13 session-scoped registry).
        There is no running loop in this thread, so a live gateway runner
        dispatches for real; without one — or for unknown verbs — this
        returns None and the verbatim text falls through to a model turn
        (the same fallback the async gateway path uses).
        """
        if kind == "command":
            try:
                from observatory.gateway_session import _dispatch_slash_command
            except Exception:
                _dispatch_slash_command = None  # type: ignore[assignment]
            if _dispatch_slash_command is not None:
                try:
                    reply = _dispatch_slash_command(
                        text, node_id=node_id, room_id=room_id or None
                    )
                except Exception:
                    reply = None
                if reply is not None:
                    return reply
        result = agent.run_conversation(text)
        if isinstance(result, dict):
            reply = result.get("final_response", "")
            if reply is None:
                return ""
            return reply if isinstance(reply, str) else str(reply)
        return "" if result is None else str(result)

    async def _run_hermes_child_turn(
        self, node_id: str, text: str, *, kind: str = "steer"
    ) -> None:
        """One headless turn on a hermes child's own session; the reply
        renders in its room, in its own voice."""
        from observatory.control import ControlNotice

        handle = self._child_handle(node_id)
        agent = getattr(handle, "agent", None) if handle is not None else None
        if agent is None:
            log.warning("hermes child turn dropped: no handle (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        room_id = ""
        try:
            if self.state is not None:
                room_id = str(self.state.get(node_id).get("room_id") or "")
        except Exception:
            room_id = ""
        lock = self._child_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            self._child_locks[node_id] = lock
        async with lock:
            try:
                reply = await asyncio.to_thread(
                    self._run_hermes_child_turn_sync,
                    agent,
                    node_id,
                    room_id,
                    text,
                    kind,
                )
            except Exception:
                log.exception("hermes child turn failed (node %s)", node_id)
                await self._post_notice(
                    ControlNotice(node_id, CHILD_PROMPT_FAILED_NOTICE)
                )
                return
        if not (reply or "").strip():
            return
        try:
            await self.renderer.render_agent_message(node_id, reply)
        except Exception:
            log.exception("child reply render failed (node %s)", node_id)

    async def _run_omp_child_prompt(self, node_id: str, text: str) -> None:
        """One omp turn (idle prompt): RPC task → summary renders in the
        child's room, in its own voice."""
        from observatory.control import ControlNotice

        try:
            handle = self._child_handle(node_id)
            rpc = getattr(handle, "rpc", None) if handle is not None else None
            if rpc is None:
                log.warning("omp child prompt dropped: no handle (node %s)", node_id)
                await self._post_notice(
                    ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE)
                )
                return
            self._ensure_omp_feed(node_id, handle)
            try:
                result = await asyncio.to_thread(rpc.run_task, text)
            except Exception:
                log.exception("omp child prompt failed (node %s)", node_id)
                await self._post_notice(
                    ControlNotice(node_id, CHILD_PROMPT_FAILED_NOTICE)
                )
                return
            reply = ""
            if isinstance(result, dict):
                reply = str(result.get("summary") or result.get("error") or "")
            elif result is not None:
                reply = str(result)
            if not reply.strip():
                return
            try:
                await self.renderer.render_agent_message(node_id, reply)
            except Exception:
                log.exception("child reply render failed (node %s)", node_id)
        finally:
            self._child_busy.discard(node_id)

    async def _steer_omp_child(self, node_id: str, text: str) -> None:
        """Mid-run steer over the child's RPC transport (fire-and-forget:
        the queued-steer notice already posted is the ack)."""
        from observatory.control import ControlNotice

        handle = self._child_handle(node_id)
        rpc = getattr(handle, "rpc", None) if handle is not None else None
        if rpc is None:
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        try:
            await asyncio.to_thread(rpc.steer, text)
        except Exception:
            log.exception("omp child steer failed (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, CHILD_STEER_FAILED_NOTICE))

    def _omp_subagent_target(self, node_id: str) -> tuple[Any | None, str]:
        """(ancestor rpc, subagent_id) for a grandchild node; (None, "")
        when unresolvable (never raises)."""
        if self.state is None:
            return None, ""
        try:
            row = self.state.get(node_id)
        except StateError:
            return None, ""
        extra = row.get("extra") or {}
        subagent_id = str(extra.get("subagent_id") or "")
        parent_id = str(row.get("parent_node_id") or "")
        if not subagent_id or not parent_id:
            return None, ""
        handle = self._child_handle(parent_id)
        rpc = getattr(handle, "rpc", None) if handle is not None else None
        return rpc, subagent_id

    async def _steer_omp_subagent(self, node_id: str, text: str) -> None:
        from observatory.control import ControlNotice

        rpc, subagent_id = self._omp_subagent_target(node_id)
        if rpc is None or not subagent_id:
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        try:
            await asyncio.to_thread(rpc.subagent_steer, subagent_id, text)
        except Exception:
            log.exception("omp subagent steer failed (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, CHILD_STEER_FAILED_NOTICE))

    async def _abort_hermes_child(self, node_id: str, reason: str) -> None:
        """Interrupt a hermes child's in-flight turn; confirm in-room."""
        from observatory.control import ControlNotice

        handle = self._child_handle(node_id)
        agent = getattr(handle, "agent", None) if handle is not None else None
        if agent is None:
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        interrupt = getattr(agent, "interrupt", None)
        if callable(interrupt):
            try:
                await asyncio.to_thread(interrupt, reason, hard_cancel=True)
            except TypeError:
                try:
                    await asyncio.to_thread(interrupt, reason)
                except Exception:
                    log.exception("hermes child interrupt failed (node %s)", node_id)
            except Exception:
                log.exception("hermes child interrupt failed (node %s)", node_id)
        try:
            self.routing_log.append(f"child-abort:{node_id}")
        except Exception:
            pass
        await self._post_notice(
            ControlNotice(node_id, STOP_CONFIRMED_NOTICE.format(status="interrupted"))
        )

    async def _abort_omp_child(self, node_id: str, reason: str) -> None:
        from observatory.control import ControlNotice

        handle = self._child_handle(node_id)
        rpc = getattr(handle, "rpc", None) if handle is not None else None
        if rpc is None:
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        abort = getattr(rpc, "abort", None)
        if callable(abort):
            try:
                await asyncio.to_thread(abort, reason)
            except TypeError:
                try:
                    await asyncio.to_thread(abort)
                except Exception:
                    log.exception("omp child abort failed (node %s)", node_id)
            except Exception:
                log.exception("omp child abort failed (node %s)", node_id)
        try:
            self.routing_log.append(f"child-abort:{node_id}")
        except Exception:
            pass
        await self._post_notice(
            ControlNotice(node_id, STOP_CONFIRMED_NOTICE.format(status="interrupted"))
        )

    async def _abort_omp_subagent(self, node_id: str, reason: str) -> None:
        from observatory.control import ControlNotice

        rpc, subagent_id = self._omp_subagent_target(node_id)
        if rpc is None or not subagent_id:
            await self._post_notice(ControlNotice(node_id, CHILD_UNAVAILABLE_NOTICE))
            return
        try:
            await asyncio.to_thread(rpc.subagent_abort, subagent_id, reason)
        except Exception:
            log.exception("omp subagent abort failed (node %s)", node_id)
        try:
            self.routing_log.append(f"child-abort:{node_id}")
        except Exception:
            pass
        await self._post_notice(
            ControlNotice(node_id, STOP_CONFIRMED_NOTICE.format(status="interrupted"))
        )

    async def _handle_gateway_abort_outcome(self, outcome: Any) -> bool:
        """BUG3: AbortSession on the gateway node — cancel the in-flight
        delivery task, hard-interrupt the cached gateway agent over the
        control socket, and post STOP_CONFIRMED. True when handled (the
        caller skips the generic notice/action loop)."""
        try:
            gw_id = self._gateway_node_id()
        except Exception:
            return False
        if getattr(outcome, "node_id", None) != gw_id:
            return False
        aborts = [a for a in (getattr(outcome, "actions", ()) or ()) if isinstance(a, AbortSession)]
        if not aborts:
            return False
        reason = str(getattr(aborts[0], "reason", "") or "matrix /stop")
        for task in list(self._gateway_tasks):
            if not task.done():
                task.cancel()
        transport = self.gateway_transport
        interrupt_out: Any = None
        if transport is not None:
            try:
                interrupt_fn = getattr(transport, "interrupt", None)
                if callable(interrupt_fn):
                    interrupt_out = await interrupt_fn(reason)
            except Exception:
                log.exception("gateway interrupt failed (node %s)", gw_id)
        status = "interrupted"
        if isinstance(interrupt_out, dict):
            if interrupt_out.get("interrupted") is False:
                status = str(interrupt_out.get("reason") or "idle — nothing to stop")
        from observatory.control import ControlNotice
        try:
            await self._post_notice(ControlNotice(gw_id, STOP_CONFIRMED_NOTICE.format(status=status)))
        except Exception:
            log.exception("stop-confirmed notice failed (node %s)", gw_id)
        self.routing_log.append(f"gateway-abort:{status}")
        return True

    def _gateway_node_id(self) -> str:
        """The gateway agent's node (router-owned when wired)."""
        router = self.control_router
        return str(getattr(router, "gateway_node_id", GATEWAY_NODE_ID) or GATEWAY_NODE_ID)

    def _is_gateway_prompt(self, outcome: Any) -> bool:
        """True when the outcome injects text into the gateway session."""
        return (
            getattr(outcome, "node_id", None) == self._gateway_node_id()
            and any(isinstance(a, InjectText) for a in (getattr(outcome, "actions", ()) or ()))
        )

    def _gateway_delivery_in_flight(self) -> bool:
        """True when a gateway prompt delivery task is currently running."""
        try:
            return any(not t.done() for t in self._gateway_tasks)
        except Exception:
            return bool(self._gateway_tasks)

    async def _maybe_post_delegate_followup(
        self, node_id: str, parent_id: str, name: str, *, status: str, summary: Any
    ) -> None:
        """Post-delegate narration: child death under the gateway node.

        When the parent is the gateway agent and no gateway delivery task
        is currently running (the turn already ended), send one labeled
        follow-up inject (kind=prompt, internal) so the gateway verifies
        the result and replies to the room. Skipped when a delivery is in
        flight — the summary then arrives via the delegate result — and
        behind the BUG2 needs-room-reply gate (routine success with
        nothing for the owner never injects). Summaries truncate to
        FOLLOWUP_SUMMARY_MAX_CHARS.
        """
        try:
            gw_id = self._gateway_node_id()
        except Exception:
            gw_id = GATEWAY_NODE_ID
        if parent_id != gw_id:
            return
        if self._gateway_delivery_in_flight():
            return
        if not _needs_room_reply(summary, status):
            log.info("delegate followup skipped: routine success (child %s status %s)", node_id, status)
            return
        transport = self.gateway_transport
        if transport is None:
            log.info("delegate followup skipped: no gateway transport (child %s)", node_id)
            return
        text_summary = str(summary or "").strip()
        if len(text_summary) > FOLLOWUP_SUMMARY_MAX_CHARS:
            text_summary = text_summary[:FOLLOWUP_SUMMARY_MAX_CHARS].rstrip() + "…"
        text = f"[subagent {name} {status}] {text_summary} verify the result and reply to the room"
        try:
            task = asyncio.create_task(
                self._deliver_gateway_prompt(gw_id, text, kind="prompt", internal=True),
                name=f"observatory-gateway-followup-{node_id}",
            )
        except RuntimeError:
            return
        self._gateway_tasks.add(task)
        task.add_done_callback(self._gateway_tasks.discard)

    async def _handle_gateway_prompt_outcome(self, outcome: Any) -> None:
        """Gateway-room text → prompt delivery (never a steer notice).

        Replies are the acknowledgement: the router's "queued steer"
        notice is skipped here (plain text starts a turn — there is no
        busy run to steer into) and the agent's reply renders when the
        turn completes. Delivery runs in its own task so the intake
        never blocks on a multi-minute turn.
        """
        for notice in outcome.notices:
            if notice.body == QUEUED_STEER_NOTICE:
                continue
            await self._post_notice(notice)
        for action in outcome.actions:
            if isinstance(action, InjectText):
                task = asyncio.create_task(
                    self._deliver_gateway_prompt(
                        str(action.node_id), str(action.text),
                        kind=str(getattr(action, "kind", None) or "prompt"),
                    ),
                    name=f"observatory-gateway-prompt-{action.node_id}",
                )
                self._gateway_tasks.add(task)
                task.add_done_callback(self._gateway_tasks.discard)
            else:
                log.info("control action pending transport: %r", action)

    async def _deliver_gateway_prompt(self, node_id: str, text: str, *, kind: str = "prompt", internal: bool = False, room_id: str | None = None) -> None:
        """One prompt → gateway session → batched replay + reply in the room."""
        from observatory.control import ControlNotice
        # Live-ingest seq-dedupe: fresh per-node live set for this turn —
        # datagrams arriving during the turn accumulate here, and the
        # batched replay below skips their seqs.
        self._gateway_live_seqs[node_id] = set()
        if internal:
            self._gateway_internal_turns[node_id] = True
        if not room_id:
            try:
                if self.state is not None:
                    room_id = str(self.state.get(node_id).get("room_id") or "") or None
            except Exception:
                room_id = None
        transport = self.gateway_transport
        if transport is None:
            log.warning("gateway prompt dropped: no transport (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, GATEWAY_UNREACHABLE_NOTICE))
            if internal:
                self._gateway_internal_turns.pop(node_id, None)
            return
        try:
            self._cot_status_event.pop(node_id, None)
        except Exception:
            pass
        router0 = self.control_router
        try:
            _cot_on_at_start = bool(router0.cot_enabled(node_id)) if router0 is not None else False
        except Exception:
            _cot_on_at_start = False
        _status_active = (not internal) and (not _cot_on_at_start)
        if _status_active:
            try:
                await self._cot_status_post(node_id, 0)
            except Exception:
                log.debug("cot status post failed (node %s)", node_id, exc_info=True)

        # BUG2: internal follow-ups post their ONE liveness notice
        # synchronously (a task races an instant fake transport and may
        # never run before cancel).
        if internal:
            try:
                await self._post_notice(
                    ControlNotice(node_id, GATEWAY_PROMPT_WORKING_NOTICE)
                )
            except Exception:
                log.debug("gateway internal liveness notice failed", exc_info=True)
            liveness = None
        else:
            async def _liveness() -> None:
                try:
                    await asyncio.sleep(GATEWAY_PROMPT_LIVENESS_AFTER_S)
                    await self._post_notice(
                        ControlNotice(node_id, GATEWAY_PROMPT_WORKING_NOTICE)
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — liveness never kills delivery
                    log.debug("gateway prompt liveness notice failed", exc_info=True)

            liveness: asyncio.Task | None = asyncio.create_task(
                _liveness(), name=f"observatory-gateway-prompt-liveness-{node_id}"
            )
        try:
            prompt_with_events = getattr(transport, "prompt_with_events", None)
            try:
                if callable(prompt_with_events):
                    try:
                        reply, events = await prompt_with_events(text, kind=kind, node_id=node_id, room_id=room_id, internal=internal)
                    except TypeError:
                        try:
                            reply, events = await prompt_with_events(text, kind=kind, node_id=node_id, internal=internal)
                        except TypeError:
                            reply, events = await prompt_with_events(text, kind=kind, node_id=node_id)
                else:
                    try:
                        reply = await transport.prompt(text, kind=kind, node_id=node_id, room_id=room_id, internal=internal)
                    except TypeError:
                        try:
                            reply = await transport.prompt(text, kind=kind, node_id=node_id, internal=internal)
                        except TypeError:
                            reply = await transport.prompt(text, kind=kind, node_id=node_id)
                    events = []
            except GatewayTransportError as exc:
                log.warning("gateway prompt delivery failed: %s", exc)
                try:
                    from gateway.control_socket import resolve_client_socket_path
                    from observatory.gateway_transport import gateway_socket_homes

                    homes = gateway_socket_homes(self.mercury_home)
                    if any(
                        resolve_client_socket_path(home) is not None
                        for home in homes
                    ):
                        await self._post_notice(
                            ControlNotice(node_id, GATEWAY_PROMPT_FAILED_NOTICE)
                        )
                    else:
                        await self._post_notice(
                            ControlNotice(node_id, GATEWAY_UNREACHABLE_NOTICE)
                        )
                except Exception:  # noqa: BLE001 — probe failure keeps old notice
                    log.debug("gateway socket probe failed", exc_info=True)
                    await self._post_notice(
                        ControlNotice(node_id, GATEWAY_UNREACHABLE_NOTICE)
                    )
                try:
                    self._cot_status_event.pop(node_id, None)
                except Exception:
                    pass
                if internal:
                    self._gateway_internal_turns.pop(node_id, None)
                return
            except asyncio.CancelledError:
                try:
                    self._cot_status_event.pop(node_id, None)
                except Exception:
                    pass
                if internal:
                    self._gateway_internal_turns.pop(node_id, None)
                raise
            except Exception:  # noqa: BLE001 — delivery never kills the task host
                log.exception("gateway prompt failed (node %s)", node_id)
                await self._post_notice(ControlNotice(node_id, GATEWAY_PROMPT_FAILED_NOTICE))
                try:
                    self._cot_status_event.pop(node_id, None)
                except Exception:
                    pass
                if internal:
                    self._gateway_internal_turns.pop(node_id, None)
                return
        finally:
            if liveness is not None and not liveness.done():
                liveness.cancel()
                try:
                    await liveness
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        assert self.renderer is not None
        # Empty-reply fix: replay first so tool history never drops on
        # empty replies. /cot status (default OFF): the turn's status was
        # posted at turn start; thinking collapsed into edits of that same
        # event. BUG2: the internal flag stays set through replay (live
        # already collapsed; replay logs without room sends), then clears.
        try:
            await self._replay_gateway_events(node_id, events or [])
            if not reply.strip():
                log.warning("gateway answered with an empty reply (node %s)", node_id)
                try:
                    self._cot_status_event.pop(node_id, None)
                except Exception:
                    pass
                return
            if self._cot_status_event.get(node_id) and cot_status_seal_short(reply):
                try:
                    if await self._cot_status_seal(node_id, reply):
                        return
                except Exception:
                    log.debug("cot status seal failed (node %s)", node_id, exc_info=True)
            try:
                self._cot_status_event.pop(node_id, None)
            except Exception:
                pass
            await self.renderer.render_agent_message(node_id, reply)
        finally:
            if internal:
                self._gateway_internal_turns.pop(node_id, None)

    async def _replay_gateway_events(self, node_id: str, events: list) -> None:
        """Batched tool/thinking replay before the final reply renders.

        Tool calls render unconditionally; thinking renders as separate
        quoted messages iff the room has thinking display on
        (``control_router.cot_enabled``) — the same gate the omp feed path
        uses. When off (default), thinking collapses into edits of the
        turn's single status message (never separate sends). Events whose
        ``seq`` was already rendered live (``_gateway_live_seqs``) are
        skipped; events without a seq always render. Unknown event shapes
        are skipped; one bad event never kills the replay. BUG2:
        ``internal`` follow-up events never send room messages (ONE
        liveness notice covers the turn) — they are recorded to the log
        only.
        """
        assert self.renderer is not None
        import json as _json

        router = self.control_router
        live = self._gateway_live_seqs.get(node_id) or set()
        for event in events or []:
            try:
                if not isinstance(event, dict):
                    continue
                seq = _coerce_live_seq(event.get("seq"))
                if seq is not None and seq in live:
                    continue
                if event.get("internal") or self._gateway_internal_turns.get(node_id):
                    log.debug("gateway replay internal collapsed (node %s seq %s type %s)", node_id, seq, event.get("type"))
                    continue
                etype = str(event.get("type") or "")
                if etype in ("tool_call", "tool"):
                    tool = str(event.get("tool") or "")
                    if not tool:
                        continue
                    args = event.get("args")
                    if args is None:
                        args_text = None
                    elif isinstance(args, str):
                        args_text = args or None
                    elif isinstance(args, dict):
                        try:
                            args_text = _json.dumps(args, default=str)
                        except Exception:
                            args_text = str(args)
                    else:
                        args_text = str(args)
                    await self.renderer.render_tool_call(node_id, tool, args_text)
                elif etype in ("thinking", "thought", "reasoning"):
                    text_val = event.get("text")
                    if not isinstance(text_val, str) or not text_val.strip():
                        continue
                    cot_on = bool(router.cot_enabled(node_id)) if router is not None else False
                    if cot_on:
                        await self.renderer.render_thinking(node_id, text_val)
                        continue
                    try:
                        s = seq if isinstance(seq, int) else 0
                    except Exception:
                        s = 0
                    try:
                        await self._cot_status_edit(node_id, s)
                    except Exception:
                        log.debug("cot status edit failed (node %s)", node_id, exc_info=True)
                    continue
            except Exception:  # noqa: BLE001 — one bad event must not kill replay
                log.exception("gateway event replay failed (node %s)", node_id)

    async def _post_notice(self, notice: Any) -> None:
        """ControlNotice → room message in the agent's own voice."""
        assert self.renderer is not None and self.state is not None
        try:
            voice = self.state.get(notice.node_id)["mxid"]
        except StateError:
            voice = self.gateway_mxid
        await self.renderer.executor.execute(
            [SendMessage(notice.node_id, voice, notice.body)]
        )

    # --- run/shutdown ---------------------------------------------------------------

    async def run(self) -> None:
        """Serve until SIGINT/SIGTERM (or ``request_stop()``)."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:  # pragma: no cover - win32
                pass
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()

    async def shutdown(self) -> None:
        """Reverse-order teardown; every step best-effort, logged. D18:
        the orchestrator registry's live handles are NEVER stopped here —
        a sidecar restart is not death; the next respawn pass re-adopts."""
        # In-flight gateway prompts first: a stale reply must not render
        # after teardown starts (renderer/client close below).
        for task in list(self._gateway_tasks):
            task.cancel()
        for task in list(self._gateway_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._gateway_tasks.clear()
        # In-flight spawned-child turns alongside them (same staleness law).
        for task in list(self._child_tasks):
            task.cancel()
        for task in list(self._child_tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._child_tasks.clear()
        try:
            self._stop_gateway_live_listener()
        except Exception:
            log.debug("gateway live listener stop failed", exc_info=True)
        for task in self._loops:
            task.cancel()
        for task in self._loops:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._loops = []
        if self._discovery_task is not None:
            self._discovery_task.cancel()
            try:
                await self._discovery_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._discovery_task = None
        if self.intake is not None:
            await self.intake.stop()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.e2ee is not None:
            # release the crypto SQLite handles (aiosqlite worker threads
            # block interpreter shutdown while a store stays open)
            await self.e2ee.stop()
            self.e2ee = None
        if self.client is not None:
            await self.client.close()
        if self._homeserver_proc is not None:
            self._homeserver_proc.terminate()
            try:
                self._homeserver_proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
                self._homeserver_proc.kill()
                self._homeserver_proc.wait()
            log.info("owned tuwunel stopped (pid %s)", self._homeserver_proc.pid)
            self._homeserver_proc = None
        if self.state is not None:
            self.state.close()  # final state flush (WAL checkpoint + close)
            self.state = None
        try:
            self._unwire_approval_ingest()
        except Exception:
            log.debug("approval ingest unwire failed", exc_info=True)
        log.info("observatory sidecar shut down cleanly")


# ============================================================================
# --once-smoke: provision + boot + intake + shutdown against a testhome
# ============================================================================

def hs_token_from_registration(registration_path: str | Path) -> str:
    """hs_token reader (the homeserver-side token the smoke uses to PUSH
    a transaction, mirroring as_token_from_registration)."""
    import yaml

    doc = yaml.safe_load(Path(registration_path).read_text(encoding="utf-8"))
    token = doc.get("hs_token") if isinstance(doc, dict) else None
    if not token:
        raise ValueError(f"no hs_token in {registration_path}")
    return str(token)


async def _smoke_push_transaction(daemon: SidecarDaemon, txn_id: str) -> None:
    """Push one transaction to the intake AS THE HOMESERVER WOULD (hs_token
    auth) carrying a plaintext message from the owner in the gateway room."""
    import aiohttp

    assert daemon.state is not None
    gw_room = daemon.state.get(GATEWAY_NODE_ID)["room_id"]
    event = {
        "type": "m.room.message",
        "event_id": f"$smoke-{txn_id}",
        "room_id": gw_room,
        "sender": daemon.owner_mxid,
        "origin_server_ts": int(time.time() * 1000),
        "content": {"msgtype": "m.text", "body": "smoke: steering probe"},
    }
    hs_token = hs_token_from_registration(daemon.paths.appservice_registration)
    async with aiohttp.ClientSession() as http:
        async with http.put(
            f"http://{HOMESERVER_ADDRESS}:{daemon.appservice_port}"
            f"/_matrix/app/v1/transactions/{txn_id}",
            params={"access_token": hs_token},
            json={"events": [event]},
        ) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"smoke txn push failed: {resp.status} {body}")


async def _smoke_e2ee_roundtrip(daemon: SidecarDaemon) -> bool:
    """MERCURY-M4C-E2EE-OK gate: encrypt one message into the gateway room
    as the gateway agent, read it back through the admin API, decrypt it,
    and compare. Requires the compiled olm stack (see e2ee.py gate)."""
    assert daemon.e2ee is not None and daemon.state is not None and daemon.client is not None
    gw = daemon.state.get(GATEWAY_NODE_ID)
    secret = f"e2ee-roundtrip-{time.time_ns()}"
    event_id = await daemon.e2ee.send_encrypted_message(
        gw["room_id"], sender=gw["mxid"], body=secret
    )
    events = await daemon.client.admin_room_messages(gw["room_id"], limit=10)
    encrypted = next(
        (e for e in events if e.get("event_id") == event_id
         and e.get("type") == "m.room.encrypted"), None
    )
    if encrypted is None:
        return False
    decrypted = await daemon.e2ee.decrypt_event(dict(encrypted))
    return bool(decrypted) and str((decrypted or {}).get("body")) == secret


def run_once_smoke(home: Path, *, fresh: bool = True) -> int:
    """Boot the FULL daemon against a throwaway testhome tuwunel, verify
    (provision + boot + intake + shutdown), print the marker."""
    import shutil

    if home.resolve() == (Path.home() / ".mercury" / "observatory").resolve():
        print("REFUSING to smoke against the real ~/.mercury/observatory", file=sys.stderr)
        return 2
    if fresh:
        shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, evidence: str = "") -> None:
        checks.append((name, ok, evidence))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {evidence}" if evidence else ""))

    daemon = SidecarDaemon(home, systemd=False)  # own the homeserver process
    marker = SMOKE_FAIL_MARKER

    async def scenario() -> None:
        nonlocal marker
        report = await daemon.boot()
        check("provision idempotent+offline-aware",
              isinstance(report.get("provision"), dict), json.dumps(report["provision"])[:120])
        check("homeserver healthy", daemon._homeserver_healthy(), daemon.base_url)
        check("D18 respawn pass ran before traffic",
              isinstance(report.get("respawn"), dict), json.dumps(report.get("respawn"))[:120])
        gw = daemon.state.get(GATEWAY_NODE_ID) if daemon.state else {}
        check("gateway node + space tree converged",
              bool(gw.get("room_id")) and bool(gw.get("space_id")),
              f"room={gw.get('room_id')} space={gw.get('space_id')} "
              f"applied={report.get('apply_plan')}")
        wired = [s for s in (daemon.control_router, daemon.approvals, daemon.directives,
                             daemon.cron_rooms, daemon.manual_runs) if s is not None]
        check("sibling subsystems wired (control/approvals/directives/cron/manual)",
              len(wired) == 5, f"{len(wired)}/5")
        check("intake endpoint live",
              daemon.intake is not None and daemon.intake.handler_attached,
              f"port {daemon.appservice_port}")
        await _smoke_push_transaction(daemon, "smoke-txn-1")
        for _ in range(40):  # ≤2s for the consumer task
            if daemon.seen_events:
                break
            await asyncio.sleep(0.05)
        seen = daemon.seen_events
        check("transaction pushed (hs_token auth) and consumed",
              any(e.get("content", {}).get("body") == "smoke: steering probe" for e in seen),
              f"{len(seen)} event(s) consumed; routing={daemon.routing_log[:3]}")
        check("discovery poll running", daemon.discovery is not None, str(daemon.hermes_db))
        if daemon.e2ee_flag:
            ok = await _smoke_e2ee_roundtrip(daemon)
            check("e2ee round-trip (encrypt → server → decrypt)", ok)
            marker = SMOKE_E2EE_MARKER if ok and all(ok for _, ok, _ in checks) else SMOKE_FAIL_MARKER
        else:
            check("e2ee flag off — plaintext path (default, honest O3 fallback)",
                  daemon.e2ee is None)
            marker = SMOKE_MARKER if all(ok for _, ok, _ in checks) else SMOKE_FAIL_MARKER

    async def lifecycle() -> None:
        try:
            await scenario()
        finally:
            await daemon.shutdown()

    asyncio.run(lifecycle())
    print(f"== {marker} ==")
    return 0 if marker in (SMOKE_MARKER, SMOKE_E2EE_MARKER) else 1


def run_repair_ghosts(home: Path | None) -> int:
    """Repair path (``mercury setup observatory`` → Install/repair, or this
    CLI): re-register every live ghost, verify each, exit non-zero unless
    all verify (gateway included)."""
    from observatory.provision import _mercury_home

    resolved = Path(home) if home is not None else _mercury_home(None)

    async def _repair() -> int:
        daemon = SidecarDaemon(resolved)
        try:
            import tomllib

            with open(daemon.paths.toml, "rb") as f:
                cfg = tomllib.load(f)["global"]
            address = cfg.get("address", HOMESERVER_ADDRESS)
            if isinstance(address, list):
                address = address[0]
            port = int(cfg.get("port", 18008))
            daemon.base_url = daemon.paths.homeserver_url(address=address, port=port)
            daemon.server_name = str(cfg.get("server_name", "mercury.local"))
            await daemon._ensure_homeserver()
            daemon.state = ObservatoryState(daemon.paths.root / "state.db")
            daemon.owner_mxid, daemon.admin_token = daemon._load_owner()
            daemon.gateway_mxid = daemon.ensure_gateway_node()
            daemon.client = MatrixClient(
                daemon.base_url,
                as_token_from_registration(daemon.paths.appservice_registration),
                server_name=daemon.server_name,
                admin_token=daemon.admin_token,
            )
            results = await daemon.repair_ghosts()
            for mxid, status in results.items():
                print(f"[{status.upper()}] {mxid}")
            bad = {m: s for m, s in results.items() if s != "verified"}
            if bad:
                print(
                    f"{len(bad)}/{len(results)} ghost(s) still unverified",
                    file=sys.stderr,
                )
                return 1
            print(f"all {len(results)} ghost(s) verified")
            return 0
        finally:
            await daemon.shutdown()

    return asyncio.run(_repair())


# ============================================================================
# CLI
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m observatory.sidecar_main",
        description="Mercury Observatory sidecar daemon (spec §2 component 2).",
    )
    parser.add_argument("--home", type=Path, default=None,
                        help="MERCURY_HOME (default: $MERCURY_HOME or ~/.mercury)")
    parser.add_argument("--once-smoke", action="store_true",
                        help="boot against a throwaway testhome, verify, exit")
    parser.add_argument("--smoke-home", type=Path, default=DEFAULT_SMOKE_HOME,
                        help="testhome for --once-smoke (default: %(default)s)")
    parser.add_argument("--repair-ghosts", action="store_true",
                        help="re-register all live ghosts, verify, exit")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.once_smoke:
        return run_once_smoke(args.smoke_home)
    if args.repair_ghosts:
        return run_repair_ghosts(args.home)

    from observatory.provision import _mercury_home

    home = args.home or _mercury_home(None)

    async def _serve() -> int:
        daemon = SidecarDaemon(home)
        try:
            await daemon.boot()
        except Exception:  # noqa: BLE001 — daemon start failures are fatal, logged
            log.exception("sidecar boot failed")
            await daemon.shutdown()
            return 1
        try:
            await daemon.run()
        finally:
            await daemon.shutdown()
        return 0

    return asyncio.run(_serve())


if __name__ == "__main__":
    sys.exit(main())
