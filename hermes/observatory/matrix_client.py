"""M3c: thin async Matrix client for the Observatory sidecar (spec §2
component 2, D3/D16).

Sync-free by design — the homeserver PUSHES transactions to the appservice
intake (:mod:`observatory.appservice`); this client only ever calls
outbound. Two surfaces:

- **Client API** authenticated with the appservice ``as_token``; virtual
  users are masqueraded via the ``?user_id=`` query param (Tuwunel
  ``src/api/router/auth/appservice.rs`` — token + user inside the
  exclusive ``@merc_.*`` namespace ⇒ acting as that user, no password).
- **Admin API** (Synapse-compatible, D16) authenticated with the OWNER's
  access token from ``owner-credentials.json`` — the as_token is not a
  user token and never admin-authorizes anything.

Every method maps 1:1 onto a call site the renderer needs. No SDK, no
retry policy, no sync: failures raise :class:`MatrixError` and sequencing
recovery belongs to the caller.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import aiohttp

from observatory.config_gen import SERVER_NAME_DEFAULT

log = logging.getLogger(__name__)

CLIENT_V3 = "/_matrix/client/v3"
CLIENT_V1 = "/_matrix/client/v1"
ADMIN_V1 = "/_synapse/admin/v1"

#: Presets (spec §4): spaces are plain private rooms; agent chat rooms use
#: trusted_private_chat so the invited owner starts at PL 100.
PRESET_PRIVATE = "private_chat"
PRESET_TRUSTED_PRIVATE = "trusted_private_chat"

#: Space room type (spec §3 — nesting is m.space.child, never threads).
SPACE_ROOM_TYPE = "m.space"


class MatrixError(RuntimeError):
    """Non-2xx Matrix response. Carries method/path/status/decoded body."""

    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method = method
        self.path = path
        self.status = status
        self.body = body if isinstance(body, dict) else {}
        self.errcode = str(self.body.get("errcode") or "")
        detail = json.dumps(self.body)[:300] if self.body is not None else ""
        super().__init__(f"{method} {path} -> HTTP {status} {self.errcode} {detail}".strip())


def _q(value: str) -> str:
    """Path-segment quote (room ids/event types contain ``! : .``)."""
    return quote(str(value), safe="")


class MatrixClient:
    """Outbound Matrix I/O for the sidecar. One aiohttp session, optional
    ownership (``async with`` closes a session it created)."""

    def __init__(
        self,
        homeserver_url: str,
        as_token: str,
        *,
        server_name: str = SERVER_NAME_DEFAULT,
        admin_token: str | None = None,
        session: aiohttp.ClientSession | None = None,
        timeout: float = 30.0,
    ):
        self.homeserver_url = homeserver_url.rstrip("/")
        self.as_token = as_token
        self.server_name = server_name
        self.admin_token = admin_token
        self._session = session
        self._owns_session = session is None
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    # --- construction ------------------------------------------------------

    @classmethod
    def from_registration(
        cls,
        registration_path: str | Path,
        homeserver_url: str,
        *,
        server_name: str = SERVER_NAME_DEFAULT,
        admin_token: str | None = None,
        **kwargs: Any,
    ) -> "MatrixClient":
        """Build from the provisioner's registration YAML — the single
        as_token source (never a second copy of the secret)."""
        from observatory.appservice import as_token_from_registration

        return cls(
            homeserver_url,
            as_token_from_registration(registration_path),
            server_name=server_name,
            admin_token=admin_token,
            **kwargs,
        )

    # --- session lifecycle --------------------------------------------------

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "MatrixClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    # --- transport -----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {token}"}
        async with self.session.request(
            method, f"{self.homeserver_url}{path}", params=params, json=json_body, headers=headers
        ) as resp:
            raw = await resp.read()
            body: Any = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body = None
            if resp.status >= 400:
                raise MatrixError(method, path, resp.status, body)
            return body

    async def client_api(
        self,
        method: str,
        path: str,
        *,
        sender: str | None = None,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Client-API call as the appservice. ``sender`` (a ``@merc_…``
        MXID) masquerades that virtual user via ``?user_id=``."""
        merged = dict(params or {})
        if sender is not None:
            merged["user_id"] = sender
        return await self._request(method, path, token=self.as_token, params=merged, json_body=json_body)

    async def admin_api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Synapse-compatible admin API as the OWNER (D16)."""
        if not self.admin_token:
            raise MatrixError(method, path, 0, {"errcode": "M_NO_ADMIN_TOKEN",
                                                "error": "admin_token not configured"})
        return await self._request(method, path, token=self.admin_token, params=params, json_body=json_body)

    # --- virtual users ---------------------------------------------------------

    async def register_virtual_user(self, localpart: str) -> str:
        """Provision a namespace ghost up-front (``m.login.application_service``).
        Idempotent in effect: M_USER_IN_USE is returned as the existing id."""
        body = {"type": "m.login.application_service", "username": localpart}
        try:
            out = await self._request(
                "POST", f"{CLIENT_V3}/register", token=self.as_token, json_body=body
            )
            return str(out.get("user_id") or "")
        except MatrixError as exc:
            if exc.errcode == "M_USER_IN_USE":
                return ""
            raise

    # --- rooms -------------------------------------------------------------------

    async def create_room(
        self,
        *,
        name: str | None = None,
        sender: str,
        preset: str | None = None,
        invite: tuple[str, ...] | list[str] = (),
        space: bool = False,
        topic: str | None = None,
    ) -> str:
        """createRoom masqueraded as ``sender`` (the room creator). Spaces
        add ``creation_content.type = m.space`` (spec §3). ``preset=None``
        derives the §4 default: private_chat for spaces,
        trusted_private_chat for rooms."""
        chosen = preset or (PRESET_PRIVATE if space else PRESET_TRUSTED_PRIVATE)
        body: dict[str, Any] = {"preset": chosen}
        if name is not None:
            body["name"] = name
        if topic is not None:
            body["topic"] = topic
        if invite:
            body["invite"] = list(invite)
        if space:
            body["creation_content"] = {"type": SPACE_ROOM_TYPE}
        out = await self.client_api("POST", f"{CLIENT_V3}/createRoom", sender=sender, json_body=body)
        room_id = str(out.get("room_id") or "")
        if not room_id:
            raise MatrixError("POST", f"{CLIENT_V3}/createRoom", 200, out)
        return room_id

    # --- messages ------------------------------------------------------------------

    async def send_message(
        self,
        room_id: str,
        body: str,
        *,
        sender: str,
        formatted_body: str | None = None,
    ) -> str:
        """m.room.message / m.text (+ org.matrix.custom.html when given)."""
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if formatted_body is not None:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = formatted_body
        return await self._send_event(room_id, content, sender=sender)

    async def edit_message(
        self,
        room_id: str,
        event_id: str,
        body: str,
        *,
        sender: str,
        formatted_body: str | None = None,
    ) -> str:
        """m.replace edit of OWN message: ``m.new_content`` carries the new
        text, outer body is the ``* `` fallback (D15 silent rolling edit)."""
        new_content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if formatted_body is not None:
            new_content["format"] = "org.matrix.custom.html"
            new_content["formatted_body"] = formatted_body
        content = {
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
        }
        return await self._send_event(room_id, content, sender=sender)

    async def _send_event(self, room_id: str, content: dict[str, Any], *, sender: str) -> str:
        # ruma/tuwunel implements the txnId PUT form (idempotent per txn) —
        # the POST-without-txnId variant 404s there.
        txn = uuid4().hex
        path = f"{CLIENT_V3}/rooms/{_q(room_id)}/send/m.room.message/{txn}"
        out = await self.client_api("PUT", path, sender=sender, json_body=content)
        event_id = str(out.get("event_id") or "")
        if not event_id:
            raise MatrixError("PUT", path, 200, out)
        return event_id

    # --- state ------------------------------------------------------------------------

    async def send_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
        *,
        sender: str,
    ) -> str:
        path = f"{CLIENT_V3}/rooms/{_q(room_id)}/state/{_q(event_type)}/{_q(state_key)}"
        out = await self.client_api("PUT", path, sender=sender, json_body=content)
        event_id = str(out.get("event_id") or "")
        if not event_id:
            raise MatrixError("PUT", path, 200, out)
        return event_id

    async def set_space_child(
        self,
        space_id: str,
        child_id: str,
        *,
        sender: str,
        via: tuple[str, ...] | list[str] = (),
        remove: bool = False,
    ) -> str:
        """``m.space.child`` add (content MUST carry ``via`` per spec) or
        remove (``{}`` content detaches the child)."""
        content: dict[str, Any] = {} if remove else {"via": list(via) or [self.server_name]}
        return await self.send_state_event(space_id, "m.space.child", child_id, content, sender=sender)

    async def get_power_levels(self, room_id: str, *, sender: str | None = None) -> dict[str, Any]:
        """Power-level content (empty state key). ``sender=None`` omits the
        ``?user_id=`` masquerade — same omission rule as :meth:`client_api`."""
        path = f"{CLIENT_V3}/rooms/{_q(room_id)}/state/m.room.power_levels/"
        return await self.client_api("GET", path, sender=sender) or {}

    async def set_power_levels(
        self,
        room_id: str,
        users: dict[str, int],
        *,
        sender: str,
    ) -> str:
        """Read-modify-write ``m.room.power_levels``: current content with
        ``users`` merged on top (D7 — owner 100 everywhere; invited users
        per config at invite time)."""
        current = await self.get_power_levels(room_id, sender=sender)
        merged = dict(current)
        merged["users"] = {**(current.get("users") or {}), **users}
        path = f"{CLIENT_V3}/rooms/{_q(room_id)}/state/m.room.power_levels/"
        return await self.client_api("PUT", path, sender=sender, json_body=merged)

    # --- membership ----------------------------------------------------------------------

    async def invite(self, room_id: str, user_id: str, *, sender: str) -> None:
        await self.client_api(
            "POST",
            f"{CLIENT_V3}/rooms/{_q(room_id)}/invite",
            sender=sender,
            json_body={"user_id": user_id},
        )

    async def join_room(self, room_id: str, *, sender: str) -> str:
        out = await self.client_api("POST", f"{CLIENT_V3}/join/{_q(room_id)}", sender=sender, json_body={})
        return str(out.get("room_id") or room_id)

    async def leave_room(self, room_id: str, *, sender: str) -> None:
        await self.client_api(
            "POST", f"{CLIENT_V3}/rooms/{_q(room_id)}/leave", sender=sender, json_body={}
        )

    # --- reads (snapshot) --------------------------------------------------------------------

    async def room_hierarchy(self, room_id: str, *, sender: str, suggested_only: bool = False) -> dict[str, Any]:
        """GET /_matrix/client/v1/rooms/{id}/hierarchy (MSC2946) — the
        space subtree incl. ``children_state`` per space; the renderer's
        snapshot source. No /sync anywhere (appservice push model)."""
        params = {"suggested_only": "true" if suggested_only else "false"}
        out = await self.client_api(
            "GET", f"{CLIENT_V1}/rooms/{_q(room_id)}/hierarchy", sender=sender, params=params
        )
        return out if isinstance(out, dict) else {}

    # --- admin (owner token) --------------------------------------------------------------------

    async def delete_room(self, room_id: str, *, block: bool = False, purge: bool = True) -> dict[str, Any]:
        """Synapse admin DELETE — the D8 purge primitive (room or space;
        a space IS a room). Tuwunel performs the delete synchronously."""
        out = await self.admin_api(
            "DELETE", f"{ADMIN_V1}/rooms/{_q(room_id)}", json_body={"block": block, "purge": purge}
        )
        return out if isinstance(out, dict) else {}

    async def admin_room(self, room_id: str) -> dict[str, Any]:
        out = await self.admin_api("GET", f"{ADMIN_V1}/rooms/{_q(room_id)}")
        return out if isinstance(out, dict) else {}

    async def admin_room_alive(self, room_id: str) -> bool:
        try:
            await self.admin_room(room_id)
            return True
        except MatrixError as exc:
            if exc.status == 404:
                return False
            raise

    async def admin_room_messages(
        self, room_id: str, *, direction: str = "b", limit: int = 100
    ) -> list[dict[str, Any]]:
        """Admin /messages read — verification + replay hardening (§9)."""
        out = await self.admin_api(
            "GET",
            f"{ADMIN_V1}/rooms/{_q(room_id)}/messages",
            params={"dir": direction, "limit": str(limit)},
        )
        events = (out or {}).get("chunk", []) if isinstance(out, dict) else []
        return list(events)
