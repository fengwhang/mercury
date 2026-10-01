"""Observatory MIRC is the PRIMARY agent surface — upstream-authorized.

The observatory perimeter (PASS-authed agent listener on
localhost/tailnet, design D7) IS this surface's authorization, exactly
like the relay's trusted upstream. Pairing/allowlist policies exist for
SECONDARY messaging platforms and must never gate the agent interface.
Public (non-observatory) MIRC keeps the ordinary allowlist policy —
that one is genuinely network-exposed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in ("IRC_ALLOWED_USERS", "IRC_ALLOW_ALL_USERS",
                "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)


def _runner(*, authorization_is_upstream: bool):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {
        Platform("irc"): SimpleNamespace(
            send=AsyncMock(),
            authorization_is_upstream=authorization_is_upstream,
            enforces_own_access_policy=False,
        )
    }
    return runner


def _source():
    return SessionSource(
        platform=Platform("irc"),
        chat_id="#nixpi4b_gateway",
        chat_type="group",
        user_id="owner",
    )


def test_observatory_mirc_is_upstream_authorized(monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    assert _runner(authorization_is_upstream=True)._is_user_authorized(
        _source()) is True


def test_public_mirc_still_default_denies(monkeypatch) -> None:
    _clear_auth_env(monkeypatch)
    assert _runner(authorization_is_upstream=False)._is_user_authorized(
        _source()) is False
