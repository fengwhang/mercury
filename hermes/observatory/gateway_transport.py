"""Sidecar side of gateway-room prompt delivery (M4a/M5c).

The control router (``observatory.control``) decides WHAT a Matrix message
means; this module delivers gateway-node prompts to the live gateway
session. Transport is the gateway control socket
(``gateway.control_socket``) — the gateway-owned, already-live IPC surface —
extended with the ``inject`` verb (params ``{text, kind, node_id}``), which
the gateway answers after running one headless turn
(``observatory.gateway_session``).

The socket home is the gateway's HERMES_HOME. In the standard layout that
IS the mercury home; in profile layouts it is ``<mercury_home>/hermes``
(the same derivation ``provision._mercury_home`` inverts). Both candidates
are probed in order — file-existence checks plus connect, no guessing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Control-socket verb the gateway answers with a headless turn
#: (registered by the gateway process at boot; see gateway/run.py seam).
GATEWAY_INJECT_VERB = "inject"

#: Prompt kinds the wire carries. The sidecar forwards the router's kind
#: (``prompt``/``steer``/``command`` — gateway-room plain text arrives
#: with the router's kind); ``gateway_session`` accepts all three.
GATEWAY_PROMPT_KIND = "prompt"

#: Opt-in per-turn ceiling (seconds) via HERMES_OBSERVATORY_INJECT_TIMEOUT_S.
HERMES_OBSERVATORY_INJECT_TIMEOUT_ENV = "HERMES_OBSERVATORY_INJECT_TIMEOUT_S"

#: No-limit default: wait until the gateway replies or the socket closes
#: (NO LIMITS law; turns run minutes). The socket default of 2s is a
#: liveness-probe budget, not a turn budget. The intake never blocks on
#: this — delivery runs in its own task.
GATEWAY_TRANSPORT_TIMEOUT_DEFAULT: float | None = None


def _inject_timeout_from_env() -> float | None:
    """Opt-in inject timeout from env; None = no limit (fail open)."""
    raw = os.environ.get(HERMES_OBSERVATORY_INJECT_TIMEOUT_ENV, "")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value


class GatewayTransportError(RuntimeError):
    """The gateway did not answer a prompt (down, unknown verb, timeout)."""


def gateway_socket_homes(mercury_home: str | Path) -> list[Path]:
    """Candidate gateway (HERMES_HOME) dirs for a mercury home, in order."""
    home = Path(mercury_home).expanduser()
    nested = home / "hermes"
    return [home] if nested == home else [home, nested]

#: Unix datagram socket the gateway uses to push live turn progress
#: (gateway_session turn collector + gateway-child feed forwarder) for
#: the sidecar's live render. Path:
#: ``$MERCURY_HOME/observatory/gateway-progress.sock``.
GATEWAY_PROGRESS_SOCK_NAME = "gateway-progress.sock"


def gateway_progress_sock_path(mercury_home: str | Path) -> Path:
    """Live-ingest socket path: ``$MERCURY_HOME/observatory/<name>``."""
    return Path(mercury_home).expanduser() / "observatory" / GATEWAY_PROGRESS_SOCK_NAME


class GatewayTransport:
    """Deliver gateway-room prompts; return the agent's reply text."""

    async def prompt(self, text: str, *, kind: str = GATEWAY_PROMPT_KIND, node_id: str = "gw") -> str:
        """Run ``text`` as a turn on the gateway session → reply text."""
        raise NotImplementedError

    async def prompt_with_events(
        self, text: str, *, kind: str = GATEWAY_PROMPT_KIND, node_id: str = "gw"
    ) -> tuple[str, list[dict[str, Any]]]:
        """Run ``text`` → (reply text, batched display events).

        Default shim for transports that only carry the reply: delegates
        to :meth:`prompt` and reports no events. The control-socket
        transport overrides this to surface the gateway's ``events`` list.
        """
        return await self.prompt(text, kind=kind, node_id=node_id), []


QueryFn = Callable[..., Optional[dict[str, Any]]]


def _default_query(
    home: Path, text: str, kind: str, node_id: str, timeout: float | None
) -> Optional[dict[str, Any]]:
    from gateway.control_socket import query_gateway_control

    return query_gateway_control(
        home,
        GATEWAY_INJECT_VERB,
        params={"text": text, "kind": kind, "node_id": node_id},
        timeout=timeout,
    )


class ControlSocketGatewayTransport(GatewayTransport):
    """Real transport: ``inject`` over the gateway control socket.

    ``query_fn(home, text, kind, node_id, timeout)`` replaces the socket
    call (tests inject a fake; production uses ``query_gateway_control``).
    Construction is cheap and side-effect free — no I/O until ``prompt``.
    """

    def __init__(
        self,
        mercury_home: str | Path,
        *,
        timeout: float | None = GATEWAY_TRANSPORT_TIMEOUT_DEFAULT,
        query_fn: Optional[QueryFn] = None,
    ) -> None:
        self.mercury_home = Path(mercury_home).expanduser()
        if timeout is None:
            timeout = _inject_timeout_from_env()
        self.timeout: float | None = None if timeout is None else float(timeout)
        self._query_fn: QueryFn = query_fn or _default_query

    async def prompt(
        self, text: str, *, kind: str = GATEWAY_PROMPT_KIND, node_id: str = "gw"
    ) -> str:
        reply, _events = await self.prompt_with_events(text, kind=kind, node_id=node_id)
        return reply

    async def prompt_with_events(
        self, text: str, *, kind: str = GATEWAY_PROMPT_KIND, node_id: str = "gw"
    ) -> tuple[str, list[dict[str, Any]]]:
        """Inject over the control socket → (reply, optional events list).

        The gateway answers ``{"reply": str, "events": [...]}``; ``events``
        is optional (absent/empty when the turn ran no tools). ``prompt``
        keeps the stable reply-only shape.
        """
        clean = (text or "").strip()
        if not clean:
            raise ValueError("gateway_transport: refusing empty prompt text")
        result = await asyncio.to_thread(
            self._query_all_homes, clean, kind, node_id
        )
        reply = result.get("reply") if isinstance(result, dict) else None
        if not isinstance(reply, str):
            raise GatewayTransportError(
                "gateway answered inject without a reply string"
            )
        raw_events = result.get("events") if isinstance(result, dict) else None
        events = raw_events if isinstance(raw_events, list) else []
        return reply, [e for e in events if isinstance(e, dict)]

    def _query_all_homes(
        self, text: str, kind: str, node_id: str
    ) -> dict[str, Any]:
        for home in gateway_socket_homes(self.mercury_home):
            try:
                result = self._query_fn(home, text, kind, node_id, self.timeout)
            except Exception as exc:  # noqa: BLE001 — one home must not kill the rest
                logger.debug("gateway_transport: home %s failed: %s", home, exc)
                continue
            if result is not None:
                return result
        raise GatewayTransportError(
            f"no gateway answered inject for {self.mercury_home} "
            "(gateway down or predates the verb)"
        )
