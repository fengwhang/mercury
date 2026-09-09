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
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Control-socket verb the gateway answers with a headless turn
#: (registered by the gateway process at boot; see gateway/run.py seam).
GATEWAY_INJECT_VERB = "inject"

#: Prompt kinds the wire carries. The sidecar always sends ``prompt``:
#: gateway-room plain text starts a turn (there is no busy run to steer).
#: ``gateway_session`` accepts the router's other kinds for stability.
GATEWAY_PROMPT_KIND = "prompt"

#: Bound on one prompt→reply round trip (turns run minutes; the socket
#: default of 2s is a liveness probe budget, not a turn budget). The
#: intake never blocks on this — delivery runs in its own task.
GATEWAY_TRANSPORT_TIMEOUT_DEFAULT = 600.0


class GatewayTransportError(RuntimeError):
    """The gateway did not answer a prompt (down, unknown verb, timeout)."""


def gateway_socket_homes(mercury_home: str | Path) -> list[Path]:
    """Candidate gateway (HERMES_HOME) dirs for a mercury home, in order."""
    home = Path(mercury_home).expanduser()
    nested = home / "hermes"
    return [home] if nested == home else [home, nested]


class GatewayTransport:
    """Deliver gateway-room prompts; return the agent's reply text."""

    async def prompt(self, text: str, *, kind: str = GATEWAY_PROMPT_KIND) -> str:
        """Run ``text`` as a turn on the gateway session → reply text."""
        raise NotImplementedError


QueryFn = Callable[..., Optional[dict[str, Any]]]


def _default_query(
    home: Path, text: str, kind: str, node_id: str, timeout: float
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
        timeout: float = GATEWAY_TRANSPORT_TIMEOUT_DEFAULT,
        query_fn: Optional[QueryFn] = None,
    ) -> None:
        self.mercury_home = Path(mercury_home).expanduser()
        self.timeout = float(timeout)
        self._query_fn: QueryFn = query_fn or _default_query

    async def prompt(
        self, text: str, *, kind: str = GATEWAY_PROMPT_KIND, node_id: str = "gw"
    ) -> str:
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
        return reply

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
