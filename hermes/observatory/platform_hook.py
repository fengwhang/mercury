"""Gateway boot seam for the MIRC observatory.

The gateway owns the whole feature in-process: state.db rows, the
OrchestratorRegistry, the RoomManager, and the MIRC adapter (bot sink).
``try_boot_sidecar`` (name kept for the run.py call site) does the sync
boot on a daemon thread; ``boot_resync`` runs after adapters connect
(join live channels, drain the frame queue, replay the exit journal,
resume omp handles, start the queue pump).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Set by :func:`try_boot_sidecar` / read by slash handlers (same process).
LAST_BOOT: Optional["BootResult"] = None


def observatory_enabled(
    config: Mapping[str, Any] | None = None,
    mercury_home: str | Path | None = None,
) -> bool:
    """Default ON; ``observatory.enabled: false`` freezes (never deletes)."""
    try:
        if config is not None:
            obs = config.get("observatory")
            if isinstance(obs, dict) and "enabled" in obs:
                return bool(obs.get("enabled"))
        from mercury_cli.config import cfg_get, load_config

        return bool(cfg_get(load_config(), "observatory", "enabled", default=True))
    except Exception:
        return True


def _load_home_config(mercury_home: str | Path | None) -> Mapping[str, Any]:
    try:
        from mercury_cli.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def mercury_home_path(mercury_home: str | Path | None = None) -> Path:
    from observatory.provision import _mercury_home

    return _mercury_home(mercury_home)


def open_state(mercury_home: str | Path | None = None) -> Any:
    from observatory.state import ObservatoryState, default_state_db_path

    return ObservatoryState(default_state_db_path(mercury_home))


@dataclass
class BootResult:
    """What a boot produced — slash handlers read these live objects."""

    mercury_home: str
    enabled: bool = True
    state: Any = None
    registry: Any = None
    manager: Any = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mercury_home": self.mercury_home,
            "enabled": self.enabled,
            "errors": list(self.errors),
        }


def boot_observatory(
    mercury_home: str | Path | None = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    registry: Any = None,
) -> BootResult:
    """Sync boot: state + registry + manager + gateway row. Never raises."""
    from observatory.provision import live_server_name
    from observatory.rooms import RoomManager, set_room_manager
    from observatory.spawn import OrchestratorRegistry

    home = mercury_home_path(mercury_home)
    result = BootResult(mercury_home=str(home))
    if not observatory_enabled(
        config if config is not None else _load_home_config(home), mercury_home=home
    ):
        result.enabled = False
        logger.info("observatory: disabled — frozen, nothing booted")
        return result
    try:
        result.state = open_state(home)
    except Exception as exc:  # noqa: BLE001 — boot must report, not raise
        result.errors.append(f"state open failed: {exc}")
        return result
    try:
        live = live_server_name(home)
        if live:
            from observatory.provision import ensure_gateway_node_in_state

            ensure_gateway_node_in_state(result.state, server_name=live)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"gateway row ensure failed: {exc}")
    try:
        prior = getattr(globals().get("LAST_BOOT"), "registry", None)
        result.registry = (
            registry
            if registry is not None
            else (prior if prior is not None else OrchestratorRegistry())
        )
    except Exception:
        from observatory.spawn import OrchestratorRegistry as _R

        result.registry = _R()
    try:
        result.manager = RoomManager(result.state)
        set_room_manager(result.manager)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"room manager build failed: {exc}")
    logger.info("observatory: boot complete — %s", result.as_dict())
    return result


def _boot_thread_body(kwargs: dict[str, Any]) -> None:
    global LAST_BOOT
    try:
        LAST_BOOT = boot_observatory(**kwargs)
    except Exception:  # noqa: BLE001 — the seam never propagates
        logger.exception("observatory: boot thread failed")


def try_boot_sidecar(
    mercury_home: str | Path | None = None, **boot_kwargs: Any
) -> Optional[threading.Thread]:
    """The gateway's one-call seam: fire-and-forget boot on a daemon
    thread. The result lands in ``LAST_BOOT``; never raises."""
    try:
        kwargs = dict(boot_kwargs)
        if mercury_home is not None:
            kwargs["mercury_home"] = mercury_home
        t = threading.Thread(
            target=_boot_thread_body,
            args=(kwargs,),
            daemon=True,
            name="mercury-observatory-boot",
        )
        t.start()
        return t
    except Exception:  # noqa: BLE001 — seam must never break the gateway
        logger.exception("observatory: try_boot_sidecar failed")
        return None


def _identity_has_live_owner(row: dict[str, Any], state: Any) -> bool:
    """Room retention is not worker liveness; native children share an owner."""
    from observatory.identity import get_pool
    from tools.async_delegation import list_delegation_children, process_identity_state

    if int(row.get("depth") or 0) == 0:
        return True
    pooled = get_pool().get(str(row.get("room_id") or "")) is not None
    visited: set[str] = set()
    while int(row.get("depth") or 0) > 0:
        if (row.get("extra") or {}).get("task_state") in {"pending", "completed"}:
            return False
        node_id = str(row.get("node_id") or "")
        if not node_id or node_id in visited:
            return False
        visited.add(node_id)
        delegation_id = node_id.split("/", 1)[0]
        child = next(
            (
                child
                for child in list_delegation_children(delegation_id)
                if child["child_id"] == node_id
            ),
            None,
        )
        if child is not None:
            return (
                child["status"] == "running"
                and process_identity_state(
                    child.get("child_pid"), child.get("child_started_at")
                )
                == "live"
            )
        parent_id = str(row.get("parent_node_id") or "")
        if not parent_id:
            break
        try:
            row = state.get(parent_id)
        except Exception:
            break
    # Same-process IRC recovery retains its existing identity and feed.
    # A new gateway has no pool: old rows alone cannot recreate live clients.
    return pooled


async def boot_resync(
    manager: Any = None, state: Any = None, registry: Any = None
) -> dict[str, Any]:
    """Post-adapter resync: join every live channel, drain the frame
    queue, replay the exit journal, resume omp handles, start the pump.

    Called once after adapters connect (and safe to re-run). Never raises.
    """
    from observatory.rooms import get_bot_sink, get_room_manager, set_room_manager
    from observatory.spawn import (
        OrchestratorRegistry,
        replay_purge_journal,
    )

    report: dict[str, Any] = {
        "joined": [],
        "resumed": [],
        "failed": [],
        "deferred_purges": [],
        "pumped": 0,
    }
    try:
        manager = manager or get_room_manager()
        boot = globals().get("LAST_BOOT")
        if state is None and boot is not None:
            state = getattr(boot, "state", None)
        if registry is None and boot is not None:
            registry = getattr(boot, "registry", None)
        if state is None:
            # No-race law: the adapter schedules this once per connect
            # while try_boot_sidecar still opens state on its thread. A
            # fresh gateway that resyncs before LAST_BOOT lands must open
            # the shared file itself — failing here leaves extra_channels
            # gateway-only, so only the gateway room ever respawns.
            try:
                home = None
                if boot is not None:
                    home = getattr(boot, "mercury_home", None) or None
                from observatory.state import default_state_db_path

                db = default_state_db_path(home)
                if db.is_file():
                    state = open_state(home)
            except Exception:
                state = None
        if manager is None and state is not None:
            from observatory.rooms import RoomManager

            manager = RoomManager(state)
            set_room_manager(manager)
        if manager is None or state is None:
            report["failed"].append("no state (unprovisioned?)")
            return report
        if registry is None:
            registry = OrchestratorRegistry()
        try:
            home_for_children = None
            if boot is not None:
                home_for_children = getattr(boot, "mercury_home", None) or None
        except Exception:
            home_for_children = None
        try:
            live = list(state.get_live())
        except Exception:
            live = []
        bot = get_bot_sink()
        reconcile = getattr(manager, "reconcile_terminal_children", None)
        if bot is not None and callable(reconcile):
            try:
                await reconcile()
            except Exception as exc:
                report["failed"].append(f"terminal marker replay: {exc}")
        try:
            # Boot self-heal (the zombie migration). The old teardown held
            # row deletion hostage to OPER DESTROY convergence, so an
            # upgraded install still carries dead rows and stale sidebar
            # entries. Reconcile them here so existing zombies are fixed,
            # not just future ones. Idempotent; never touches history.
            import asyncio as _asyncio2

            from observatory.room_reaper import reap_orphan_rooms

            report["reaped"] = await _asyncio2.to_thread(
                reap_orphan_rooms,
                state,
                mercury_home=home_for_children,
                live_channels=[
                    str(row.get("room_id") or "")
                    for row in live
                    if int(row.get("depth") or 0) == 0
                ],
            )
        except Exception as exc:
            report["failed"].append(f"room reap: {exc}")
        # Reconciliation changes the tree. Never JOIN or recreate identities
        # from the pre-reconciliation snapshot of terminal children.
        try:
            live = list(state.get_live())
        except Exception:
            live = []
        mlounge_nick = ""
        try:
            from observatory.provision import get_mlounge_nick as _mlounge_nick

            mlounge_nick = str(_mlounge_nick(None) or "")
        except Exception:
            mlounge_nick = ""
        for row in live:
            try:
                channel = str((row or {}).get("room_id") or "")
                if not channel:
                    continue
                if not _identity_has_live_owner(row, state):
                    continue
                if bot is not None:
                    try:
                        if await bot.join_channel(channel):
                            report["joined"].append(channel)
                            try:
                                from observatory.identity import ensure_identity

                                nick = str((row or {}).get("mxid") or "")
                                extra = (row or {}).get("extra") or {}
                                is_gateway = str(
                                    (row or {}).get("node_id") or ""
                                ) == "gw" or (
                                    isinstance(extra, dict)
                                    and extra.get("kind") == "gateway"
                                )
                                # The gateway already owns the receive/dispatch
                                # connection. A send-only clone reclaims its nick
                                # and disconnects inbound for EVERY agent room.
                                if nick and not is_gateway:
                                    await ensure_identity(nick, channel)
                            except Exception:
                                pass
                            try:
                                # Spawn parity: the bot JOIN recreates the
                                # channel server-side and the fanout pulls
                                # in connected clients, but an explicit
                                # INVITE is the nudge The Lounge needs —
                                # without it a restart leaves the user
                                # with just the lobby.
                                if mlounge_nick:
                                    await bot.invite_user(mlounge_nick, channel)
                            except Exception:
                                pass
                    except Exception:
                        pass
                # Resume omp handles the registry lost (restart crash).
                if (
                    str((row or {}).get("engine") or "") == "omp"
                    and str((row or {}).get("status") or "") == "live"
                    and int((row or {}).get("depth") or 0) == 0
                ):
                    node_id = str((row or {}).get("node_id") or "")
                    try:
                        if registry.get(node_id) is None:
                            import asyncio as _asyncio

                            from observatory.spawn import resurrect_omp_handle

                            # Off the gateway loop: process spawn + RPC
                            # handshake block for tens of seconds (slow
                            # hosts stall PINGs past the server's idle
                            # drop while the loop is frozen).
                            await _asyncio.to_thread(
                                resurrect_omp_handle,
                                state=state,
                                registry=registry,
                                node_id=node_id,
                                channel=channel,
                                mercury_home=home_for_children,
                            )
                            report["resumed"].append(node_id)
                    except Exception as exc:
                        report["failed"].append(f"{node_id}: {exc}")
            except Exception:
                continue
        try:
            deferred = await replay_purge_journal(state)
            report["deferred_purges"] = deferred
        except Exception as exc:
            report["failed"].append(f"journal replay: {exc}")
    except Exception as exc:  # noqa: BLE001 — resync never breaks the gateway
        report["failed"].append(str(exc))
    try:
        # Resync completion marker: `mercury observatory restart`
        # polls this to prove the fleet actually respawned (fresh
        # timestamp + per-channel results) instead of assuming the
        # gateway restart was sufficient. Written on every run that
        # reaches state — including partial failures (failed list
        # non-empty); a missing/stale marker means resync never ran.
        import json as _json
        import time as _time

        if state is not None:
            state.set_meta(
                "last-resync",
                _json.dumps({
                    "epoch": _time.time(),
                    "joined": sorted(set(report.get("joined") or [])),
                    "resumed": sorted(set(report.get("resumed") or [])),
                    "failed": list(report.get("failed") or []),
                }),
            )
    except Exception:
        pass
    return report
