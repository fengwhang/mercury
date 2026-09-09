"""M5a (matrix observatory §2 component 3): the gateway integration seam.

The one place the GATEWAY process touches the observatory at startup —
post-provision, pre-traffic. Everything here is an importable pure-ish
function over ``$MERCURY_HOME``; the long-lived sidecar daemon (appservice
HTTP endpoint, transaction intake, control router, feed loops —
``sidecar_main``, a later milestone) is assembled FROM these builders, not
replaced by them.

What boot does (D18 ordering — recovery before traffic):
1. open the observatory state store (``<MERCURY_HOME>/observatory/state.db``);
2. build the renderer IF a matrix client is supplied (the sidecar_main /
   provisioned path; without one this is a state-only boot);
3. replay the write-ahead purge journal (crashed /exit purges finish);
4. run the respawn pass (live 0-agents resume; rooms/spaces re-ensured).

THE GATEWAY SEAM (for the sidecar_main agent — one additive insertion in
``gateway/run.py``'s ``start_gateway``, placed AFTER platform adapters
connect and BEFORE ``await self._finish_startup_restore()`` opens the
inbound gate, i.e. post-provision / pre-traffic, ~line 14283):

    try:  # M5a observatory seam (spec §2 component 3)
        from observatory.platform_hook import try_boot_sidecar
        try_boot_sidecar()
    except ImportError:
        pass

``try_boot_sidecar`` never raises and never blocks the loop: it runs the
async boot on a private event loop in a daemon thread (the respawn pass
spawns real omp subprocesses; the gateway loop must not stall on them),
stores the result on this module (``LAST_BOOT``) for the sidecar daemon to
adopt, and logs one line either way. Full in-process wiring (aiohttp
appservice, discovery stream consumption) lands with sidecar_main, which
calls :func:`boot_sidecar` directly on the gateway loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Set by :func:`try_boot_sidecar` / read by the sidecar daemon to adopt a
#: boot that already happened on the gateway thread (idempotent handoff).
LAST_BOOT: Optional["BootResult"] = None


def observatory_enabled(
    config: Optional[Mapping[str, Any]] = None,
    mercury_home: str | Path | None = None,
) -> bool:
    """D1: default ON; ``observatory.enabled: false`` freezes (never
    deletes) — the hook then boots nothing and leaves all durable state
    untouched for the next re-enable."""
    if config is None:
        config = _load_home_config(mercury_home)
    obs = config.get("observatory") if isinstance(config, Mapping) else None
    enabled = (obs or {}).get("enabled") if isinstance(obs, Mapping) else None
    if enabled is None:
        return True  # D1: absent key defaults ON
    return bool(enabled) and str(enabled).strip().lower() not in (
        "0", "false", "no", "off",
    )


def _load_home_config(mercury_home: str | Path | None) -> Mapping[str, Any]:
    """Read ``<mercury_home>/config.yaml`` when a home is given (the boot
    seam must honor the home it was passed, not ambient env); fall back
    to the CLI loader (MERCURY_CONFIG resolution)."""
    if mercury_home is not None:
        try:
            import yaml

            doc = yaml.safe_load(
                (Path(mercury_home) / "config.yaml").read_text(encoding="utf-8")
            )
            if isinstance(doc, Mapping):
                return dict(doc)
        except (OSError, ValueError):
            logger.debug("observatory: %s/config.yaml unreadable — CLI config path",
                         mercury_home)
        except Exception:  # noqa: BLE001 — broken config must not wedge boot
            logger.exception("observatory: config load failed — treating as enabled")
    try:
        from mercury_cli.config import load_config

        cfg = load_config()
        return cfg if isinstance(cfg, Mapping) else {}
    except Exception:  # noqa: BLE001
        logger.exception("observatory: config load failed — treating as enabled")
        return {}


# ============================================================================
# Component builders (pure constructors — no I/O beyond path derivation)
# ============================================================================


def mercury_home_path(mercury_home: str | Path | None = None) -> Path:
    """Resolve $MERCURY_HOME exactly like provisioning does (never a
    second derivation)."""
    from observatory.provision import _mercury_home

    return _mercury_home(mercury_home)


def hermes_state_db_path(mercury_home: str | Path | None = None) -> Path:
    """The hermes engine's state.db (async_delegations + sessions) inside
    the ONE state tree — discovery's poll source."""
    return mercury_home_path(mercury_home) / "hermes" / "state.db"


def open_state(mercury_home: str | Path | None = None) -> Any:
    """Open :class:`ObservatoryState` at the canonical observatory path."""
    from observatory.state import ObservatoryState, default_state_db_path

    return ObservatoryState(default_state_db_path(mercury_home))


def build_discovery(mercury_home: str | Path | None = None, **engine_kwargs: Any) -> Any:
    """:class:`DiscoveryEngine` over the hermes state.db (§7 poll source).
    Not started — the sidecar daemon owns loop lifetimes."""
    from observatory.discovery import DiscoveryEngine

    return DiscoveryEngine(hermes_state_db_path(mercury_home), **engine_kwargs)


def build_renderer(
    state: Any,
    *,
    client: Any,
    gateway_node_id: str,
    owner_mxid: str,
    server_name: str = "mercury.local",
) -> Any:
    """Renderer + IntentExecutor bound to a live MatrixClient (the
    appservice token / admin token come from the provisioned home —
    sidecar_main's job to read; this stays a pure constructor)."""
    from observatory.renderer import IntentExecutor, Renderer

    return Renderer(
        state,
        gateway_node_id=gateway_node_id,
        server_name=server_name,
        owner_mxid=owner_mxid,
        executor=IntentExecutor(
            client, state, owner_mxid=owner_mxid, server_name=server_name
        ),
    )


# ============================================================================
# Boot
# ============================================================================


@dataclass
class BootResult:
    """What a boot produced — the sidecar daemon adopts these handles."""

    mercury_home: str
    enabled: bool = True
    state: Any = None
    renderer: Any = None
    registry: Any = None
    report: Any = None  # RespawnReport
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        report = getattr(self.report, "as_dict", None)
        return {
            "mercury_home": self.mercury_home,
            "enabled": self.enabled,
            "resumed": list(getattr(self.report, "resumed", []) or []),
            "skipped": list(getattr(self.report, "skipped", []) or []),
            "failed": list(getattr(self.report, "failed", []) or []),
            "deferred_purges": list(getattr(self.report, "deferred_purges", []) or []),
            "errors": list(self.errors),
        }


async def boot_sidecar(
    mercury_home: str | Path | None = None,
    *,
    client: Any = None,
    gateway_node_id: str = "gw",
    owner_mxid: str = "",
    server_name: str = "mercury.local",
    discovery: bool = True,
    config: Optional[Mapping[str, Any]] = None,
    registry: Any = None,
) -> BootResult:
    """The real boot (callable from any loop — sidecar_main calls it on
    the gateway loop). Builds state + renderer (when ``client`` given),
    replays the purge journal and runs the respawn pass. The discovery
    engine is CONSTRUCTED here (ready to start) but not started: its
    poll task lifecycle belongs to the daemon."""
    home = mercury_home_path(mercury_home)
    result = BootResult(mercury_home=str(home))

    if not observatory_enabled(config, mercury_home=home):
        result.enabled = False
        logger.info("observatory: disabled (D1) — frozen, nothing booted")
        return result

    try:
        result.state = open_state(home)
    except Exception as exc:  # noqa: BLE001 — boot must report, not raise
        result.errors.append(f"state open failed: {exc}")
        logger.exception("observatory: state open failed")
        return result

    if client is not None:
        try:
            result.renderer = build_renderer(
                result.state,
                client=client,
                gateway_node_id=gateway_node_id,
                owner_mxid=owner_mxid,
                server_name=server_name,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"renderer build failed: {exc}")
            logger.exception("observatory: renderer build failed")

    from observatory.respawn import respawn_pass
    from observatory.spawn import OrchestratorRegistry

    result.registry = registry if registry is not None else OrchestratorRegistry()
    try:
        result.report = await respawn_pass(
            state=result.state,
            registry=result.registry,
            renderer=result.renderer,
            mercury_home=home,
        )
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"respawn pass failed: {exc}")
        logger.exception("observatory: respawn pass failed")

    if discovery:
        try:
            build_discovery(home)  # construct-only (see docstring)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"discovery build failed: {exc}")
            logger.exception("observatory: discovery build failed")

    logger.info("observatory: boot complete — %s", result.as_dict())
    return result


def _boot_thread_body(kwargs: dict[str, Any]) -> None:
    global LAST_BOOT
    try:
        LAST_BOOT = asyncio.run(boot_sidecar(**kwargs))
    except Exception:  # noqa: BLE001 — the seam never propagates
        logger.exception("observatory: sidecar boot thread failed")


def try_boot_sidecar(
    mercury_home: str | Path | None = None, **boot_kwargs: Any
) -> Optional[threading.Thread]:
    """The gateway's one-call seam: fire-and-forget boot on a daemon
    thread (respawn spawns real omp subprocesses — the gateway loop must
    not stall on them). The result lands in ``LAST_BOOT`` when the thread
    finishes; sidecar_main adopts it. Never raises; returns the thread."""
    try:
        kwargs = dict(boot_kwargs)
        if mercury_home is not None:
            kwargs["mercury_home"] = mercury_home
        t = threading.Thread(
            target=_boot_thread_body, args=(kwargs,), daemon=True,
            name="mercury-observatory-boot",
        )
        t.start()
        return t
    except Exception:  # noqa: BLE001 — seam must never break the gateway
        logger.exception("observatory: try_boot_sidecar failed")
        return None
