"""Application-service transaction intake for the Observatory sidecar
(spec §2 component 2, D3).

A thin aiohttp server (already a hermes dependency — no new deps):

- ``PUT /_matrix/app/v1/transactions/{txnId}`` — homeserver pushes event
  batches. Responds 200 ALWAYS once the transaction is accepted (any
  non-2xx makes the homeserver retry forever). When no consumer is
  attached yet the body carries the ``M_NOT_YET_SENT`` stub marker:
  accepted + queued; the sidecar daemon attaches the renderer/discovery
  consumer at boot.
- ``GET /health`` — unauthenticated liveness probe.
- Token-check middleware: every other route requires the appservice
  ``as_token`` (``Authorization: Bearer`` header or ``access_token``
  query param, constant-time compare) → 401 ``M_UNKNOWN_TOKEN`` otherwise.

Transactions are deduplicated by txnId (homeservers may retry a txn after
a network blip even though we answered 200) and dispatched to an
``asyncio.Queue`` consumed by a handler callback. This module is the
intake only — mautrix remains the full appservice framework in later
milestones; keep it replaceable.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from observatory.config_gen import (
    APPSERVICE_PORT_DEFAULT,
    HOMESERVER_ADDRESS,
)

log = logging.getLogger(__name__)

#: Matrix appservice API paths (spec: HS → AS push).
TRANSACTIONS_PATH = r"/_matrix/app/v1/transactions/{txn_id}"
HEALTH_PATH = "/health"

#: Bounded txnId memory: dedup window for homeserver retries.
TXN_MEMORY_DEFAULT = 1024

EventHandler = Callable[[str, list[dict[str, Any]]], Awaitable[None]]

#: Raw appservice crypto fields (``to_device`` / ``device_lists`` /
#: ``device_one_time_keys_count``) routed to the E2EE machines alongside
#: the room events. A separate callback — not a third handler arg — so
#: every existing 2-arg EventHandler keeps working untouched.
CryptoHandler = Callable[[dict[str, Any]], Awaitable[None]]


class TransactionIntake:
    """Dedup + queue between the HTTP surface and the event consumer."""

    def __init__(
        self,
        *,
        as_token: str,
        handler: EventHandler | None = None,
        crypto_handler: CryptoHandler | None = None,
        queue_size: int = 1000,
        txn_memory: int = TXN_MEMORY_DEFAULT,
    ):
        if not as_token:
            raise ValueError("as_token must be a non-empty secret")
        self.as_token = as_token
        self.queue: asyncio.Queue[
            tuple[str, list[dict[str, Any]], dict[str, Any]]
        ] = asyncio.Queue(maxsize=queue_size)
        self._handler: EventHandler | None = handler
        self._crypto_handler: CryptoHandler | None = crypto_handler
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._txn_memory = txn_memory
        self._consumer: asyncio.Task[None] | None = None

    # --- dedup ----------------------------------------------------------------

    def register_txn(self, txn_id: str) -> bool:
        """Record a txnId; True on FIRST sight, False for a retry (dedup)."""
        if txn_id in self._seen:
            self._seen.move_to_end(txn_id)
            return False
        self._seen[txn_id] = None
        while len(self._seen) > self._txn_memory:
            self._seen.popitem(last=False)
        return True

    # --- consumption ------------------------------------------------------------

    @property
    def handler_attached(self) -> bool:
        return self._handler is not None

    def attach_handler(self, handler: EventHandler) -> None:
        """Set the consumer callback; takes effect on next ``start()``."""
        self._handler = handler

    def attach_crypto_handler(self, handler: CryptoHandler | None) -> None:
        """Set the crypto-fields callback (to-device/device-lists/OTK
        counts); takes effect on next ``start()``."""
        self._crypto_handler = handler

    async def start(self) -> None:
        """Spawn the consumer task (no-op without a handler attached)."""
        if self._handler is None or self._consumer is not None:
            return
        self._consumer = asyncio.create_task(
            self._consume(), name="observatory-txn-consumer"
        )

    async def stop(self) -> None:
        if self._consumer is not None:
            self._consumer.cancel()
            try:
                await self._consumer
            except asyncio.CancelledError:
                pass
            self._consumer = None

    async def _consume(self) -> None:
        assert self._handler is not None
        while True:
            txn_id, events, crypto = await self.queue.get()
            try:
                # crypto FIRST: to-device room keys must land before the
                # room events that need them reach decrypt.
                if crypto and self._crypto_handler is not None:
                    await self._crypto_handler(crypto)
                await self._handler(txn_id, events)
            except Exception:  # consumer bugs must never kill the intake
                log.exception("observatory event handler failed for txn %s", txn_id)
            finally:
                self.queue.task_done()

    # --- intake -----------------------------------------------------------------

    async def accept(self, txn_id: str, events: list[dict[str, Any]],
                     crypto: dict[str, Any] | None = None) -> web.Response:
        """Dedup, enqueue, and answer. 200 in every accepted case — the
        homeserver treats non-2xx as "retry forever"."""
        if not isinstance(events, list):
            return web.json_response(
                {"errcode": "M_BAD_JSON", "error": "events must be a list"},
                status=400,
            )
        if not self.register_txn(txn_id):
            return web.json_response({})  # retry of an accepted txn: idempotent 200
        try:
            self.queue.put_nowait((txn_id, events, dict(crypto or {})))
        except asyncio.QueueFull:
            # Real backpressure: 429 is the canonical homeserver retry signal.
            return web.json_response(
                {"errcode": "M_LIMIT_EXCEEDED", "error": "intake queue full"},
                status=429,
            )
        if not self.handler_attached:
            return web.json_response(
                {
                    "errcode": "M_NOT_YET_SENT",
                    "error": "transaction accepted; no event consumer attached",
                }
            )
        return web.json_response({})


# --- HTTP wiring ------------------------------------------------------------------

#: Typed application key (aiohttp AppKey — string keys warn on 3.9+).
_INTAKE_KEY = web.AppKey("intake", TransactionIntake)


@web.middleware
async def _token_middleware(request: web.Request, handler):
    if request.path == HEALTH_PATH:
        return await handler(request)
    token = request.query.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
    if not token or not hmac.compare_digest(token, request.app[_INTAKE_KEY].as_token):
        return web.json_response(
            {"errcode": "M_UNKNOWN_TOKEN", "error": "invalid or missing as_token"},
            status=401,
        )
    return await handler(request)


async def _put_transaction(request: web.Request) -> web.Response:
    txn_id = request.match_info["txn_id"]
    if not txn_id:
        return web.json_response(
            {"errcode": "M_UNRECOGNIZED", "error": "empty transaction id"},
            status=404,
        )
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return web.json_response(
            {"errcode": "M_NOT_JSON", "error": "request body is not JSON"},
            status=400,
        )
    intake: TransactionIntake = request.app[_INTAKE_KEY]
    # Crypto side-channel (E2EE key-sharing transport): the ruma/tuwunel
    # appservice extension carries to-device messages, device-list deltas
    # and OTK counts beside the room events. Only present keys ride along
    # (handle_as_transaction reads the same raw shape).
    crypto = {key: body[key] for key in (
        "to_device", "device_lists", "device_one_time_keys_count",
        "device_one_time_keys_counts",
    ) if key in body and body[key]}
    return await intake.accept(txn_id, body.get("events", []), crypto=crypto)


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def make_app(intake: TransactionIntake) -> web.Application:
    """Wire routes + token middleware onto a fresh application."""
    app = web.Application(middlewares=[_token_middleware])
    app[_INTAKE_KEY] = intake
    app.router.add_put(TRANSACTIONS_PATH, _put_transaction)
    app.router.add_get(HEALTH_PATH, _health)
    return app


def as_token_from_registration(registration_path: str | Path) -> str:
    """Read the ``as_token`` the provisioner wrote into the appservice
    registration YAML (config_gen.render_appservice_registration_yaml) —
    the single source of truth, never a second copy of the secret."""
    import yaml

    doc = yaml.safe_load(Path(registration_path).read_text(encoding="utf-8"))
    token = doc.get("as_token") if isinstance(doc, dict) else None
    if not token:
        raise ValueError(f"no as_token in {registration_path}")
    return str(token)


def serve(
    intake: TransactionIntake,
    *,
    host: str = HOMESERVER_ADDRESS,
    port: int = APPSERVICE_PORT_DEFAULT,
) -> None:
    """Foreground entry point for the systemd unit
    (mercury-observatory.service)."""
    app = make_app(intake)

    async def _on_start(app: web.Application) -> None:
        await intake.start()

    async def _on_cleanup(app: web.Application) -> None:
        await intake.stop()

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=host, port=port, print=lambda *a: None)
