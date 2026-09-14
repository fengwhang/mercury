"""Gateway boot seam for the IRC observatory.

The gateway owns the whole feature in-process: state.db rows, the
OrchestratorRegistry, the RoomManager, and the IRC adapter (bot sink).
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
        build_omp_child,
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
            live = list(state.get_live())
        except Exception:
            live = []
        bot = get_bot_sink()
        for row in live:
            try:
                channel = str((row or {}).get("room_id") or "")
                if not channel:
                    continue
                if bot is not None:
                    try:
                        if await bot.join_channel(channel):
                            report["joined"].append(channel)
                            try:
                                from observatory.soju import subscribe_user_channel

                                subscribe_user_channel(channel)
                            except Exception:
                                pass
                            try:
                                from observatory.identity import ensure_identity

                                nick = str((row or {}).get("mxid") or "")
                                if nick:
                                    await ensure_identity(nick, channel)
                            except Exception:
                                pass
                    except Exception:
                        pass
                # Resume omp handles the registry lost (restart crash).
                if (
                    str((row or {}).get("engine") or "") == "omp"
                    and str((row or {}).get("status") or "") == "live"
                ):
                    node_id = str((row or {}).get("node_id") or "")
                    try:
                        if registry.get(node_id) is None:
                            ref = str((row or {}).get("session_ref") or "")
                            child = build_omp_child(resume_session=ref or None)
                            from observatory.spawn import OrchestratorHandle

                            registry.register(
                                OrchestratorHandle(
                                    node_id=node_id,
                                    engine="omp",
                                    name=str((row or {}).get("name") or node_id),
                                    session_ref=ref,
                                    rpc=child,
                                )
                            )
                            from observatory.rooms import register_omp_room

                            register_omp_room(node_id, channel, child)
                            report["resumed"].append(node_id)
                    except Exception as exc:
                        report["failed"].append(f"{node_id}: {exc}")
            except Exception:
                continue
        try:
            report["pumped"] = await manager.drain_queue()
        except Exception:
            pass
        try:
            # Lobby self-heal: restarts/upgrades must never leave the
            # phone without its gateway room (no setup run required).
            from observatory.provision import live_server_name
            from observatory.rooms import gateway_channel
            from observatory.soju import subscribe_user_channel

            lobby = gateway_channel(live_server_name(None) or "mercury")
            if subscribe_user_channel(lobby):
                report["lobby"] = lobby
                try:
                    # Nudge already-connected phones: subscription alone
                    # only surfaces on (re)connect, the INVITE taps now.
                    from observatory.soju import SOJU_USER

                    if bot is not None and await bot.invite_user(SOJU_USER, lobby):
                        report["lobby_invited"] = True
                except Exception:
                    pass
        except Exception:
            pass
        try:
            deferred = await replay_purge_journal(state)
            report["deferred_purges"] = deferred
        except Exception as exc:
            report["failed"].append(f"journal replay: {exc}")
        try:
            from observatory import rooms as _rooms

            await _rooms.start_pump(manager)
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001 — resync never breaks the gateway
        report["failed"].append(str(exc))
    return report
