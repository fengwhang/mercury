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
   the remaining engine transports (RPC steer/prompt fan-out, aborts)
   are still pending — those ACTIONS stay logged in ``routing_log``.
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
from observatory.control import QUEUED_STEER_NOTICE, InjectText
from observatory.gateway_transport import (
    ControlSocketGatewayTransport,
    GatewayTransportError,
)
from observatory.renderer import IntentExecutor, Renderer, SendMessage
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
        self._runner: web.AppRunner | None = None
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
        #: Rooms already carrying a decrypt-failure recovery notice (one
        #: notice per room per process — failures after the first only log).
        self._decrypt_notified: set[str] = set()
        self.omp_feeds: dict[str, Any] = {}  # node_id -> OmpFeed

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

        # 7. sibling subsystems (M4a/M4b/M5) — integrate, never edit
        self.wire_siblings()
        # directives membership reconcile + D7 snapshot warm (BEFORE the
        # intake serves: first steer must not fail-closed on a cold cache)
        await self._sync_directives_membership()
        await self._refresh_power_levels()
        report["directives_members"] = len(self._directives_members())

        # 8. intake endpoint (this loop) — LAST: traffic only after recovery
        await self._serve_intake()

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
        logged in ``routing_log``."""
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

        self.control_router = ControlRouter(
            self.state,
            gateway_node_id=GATEWAY_NODE_ID,
            pl_provider=pl_snapshot,
        )
        self.approvals = ApprovalBridge(
            state=self.state,
            poster=self.client,
            authority=MatrixAuthority(self.client, reader_mxid=self.gateway_mxid),
        )
        # Gateway-session prompt transport (control-socket ``inject``).
        # Construction is side-effect free (no I/O until a prompt sends);
        # None only when the package itself is unavailable.
        try:
            self.gateway_transport = ControlSocketGatewayTransport(self.mercury_home)
        except Exception:  # noqa: BLE001 — delivery reports unreachable instead
            log.exception("gateway transport unavailable (prompts will not deliver)")
            self.gateway_transport = None
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
    def _attach_omp_feeds(self) -> None:
        """One OmpFeed per live spawned omp RPC child (registry handles).
        Grandchildren render into their own rooms; the subagent→node map
        lives per feed."""
        from observatory.omp_feed import OmpFeed

        assert self.registry is not None
        for handle in self.registry.handles():
            rpc = getattr(handle, "rpc", None)
            if rpc is None or handle.node_id in self.omp_feeds:
                continue
            feed = OmpFeed(rpc)
            self.omp_feeds[handle.node_id] = feed
            self._loops.append(
                asyncio.create_task(
                    self._run_omp_feed(handle.node_id, feed),
                    name=f"observatory-omp-feed-{handle.node_id}",
                )
            )

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
            if row["status"] == "live":
                await self.renderer.render_death(
                    node_id, status=event.status, summary=event.summary
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
        intake never dies on a notice failure. Never raises.
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
            from observatory.e2ee import decrypt_failure_notice

            await self.client.send_message(
                room_id, decrypt_failure_notice(event_id, room_id),
                sender=self.gateway_mxid)
        except Exception:  # noqa: BLE001 — notices never kill the intake
            log.exception("decrypt-failure notice failed")

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
        if self.approvals is not None:
            action = await self.approvals.handle_event(event)
            if action is not None:
                self.routing_log.append(f"approvals:{action}")
        # M4a control routing (steer/stop/verbs/commands)
        if self.control_router is not None:
            outcomes = await self.control_router.handle_transaction(txn_id, [event])
            for outcome in outcomes:
                self.routing_log.append(outcome.disposition)
                if self._is_gateway_prompt(outcome):
                    await self._handle_gateway_prompt_outcome(outcome)
                    continue
                for notice in outcome.notices:
                    await self._post_notice(notice)
                for action in outcome.actions:
                    # Engine transports (gateway WS injection, RPC steer)
                    # land with the M4a/M5 gateway-side wiring — logged here.
                    log.info("control action pending transport: %r", action)

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
                    ),
                    name=f"observatory-gateway-prompt-{action.node_id}",
                )
                self._gateway_tasks.add(task)
                task.add_done_callback(self._gateway_tasks.discard)
            else:
                log.info("control action pending transport: %r", action)

    async def _deliver_gateway_prompt(self, node_id: str, text: str) -> None:
        """One prompt → gateway session → reply renders in the room."""
        from observatory.control import ControlNotice

        transport = self.gateway_transport
        if transport is None:
            log.warning("gateway prompt dropped: no transport (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, GATEWAY_UNREACHABLE_NOTICE))
            return
        try:
            reply = await transport.prompt(text, kind="prompt", node_id=node_id)
        except GatewayTransportError as exc:
            log.warning("gateway prompt delivery failed: %s", exc)
            await self._post_notice(ControlNotice(node_id, GATEWAY_UNREACHABLE_NOTICE))
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — delivery never kills the task host
            log.exception("gateway prompt failed (node %s)", node_id)
            await self._post_notice(ControlNotice(node_id, GATEWAY_PROMPT_FAILED_NOTICE))
            return
        if not reply.strip():
            log.warning("gateway answered with an empty reply (node %s)", node_id)
            return
        assert self.renderer is not None
        await self.renderer.render_agent_message(node_id, reply)

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
