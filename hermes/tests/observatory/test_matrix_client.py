"""Contract tests for the Observatory Matrix client (M3c, spec §2/D3/D16).

Mock aiohttp homeserver: every request is captured (method, path, query,
auth header, body) and answered from canned route handlers. Laws under
test: client-API masquerade via ?user_id= with the as_token, admin-API
auth with the OWNER token, exact paths, m.space creation content, edit
relates_to shape, power-level read-modify-write, admin DELETE purge, and
the sync-free surface (hierarchy is the only read).
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from observatory.matrix_client import MatrixClient, MatrixError

AS_TOKEN = "as-token-1"
ADMIN_TOKEN = "owner-token-1"
SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
AGENT = "@merc_auth-refactor:mercury.local"

ROOM_ID = "!room1:mercury.local"
EVENT_ID = "$evt1"
BASE_PL = {
    "users": {AGENT: 100},
    "users_default": 0,
    "events": {"m.room.name": 50},
    "events_default": 0,
    "state_default": 50,
    "invite": 0,
    "kick": 50,
    "ban": 50,
    "redact": 50,
}


class MockHomeserver:
    """Records requests; canned responses per route."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        app = web.Application()
        for method, path in [
            ("POST", r"/_matrix/client/v3/createRoom"),
            ("POST", r"/_matrix/client/v3/register"),
            ("PUT", r"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}"),
            ("GET", r"/_matrix/client/v3/rooms/{rid}/state/{etype}/{skey}"),
            # Power levels use an empty state key (trailing slash): aiohttp
            # {skey} requires a non-empty segment, so this needs its own route.
            ("GET", r"/_matrix/client/v3/rooms/{rid}/state/m.room.power_levels/"),
            ("PUT", r"/_matrix/client/v3/rooms/{rid}/state/m.room.power_levels/"),
            ("PUT", r"/_matrix/client/v3/rooms/{rid}/state/{etype}/{skey}"),
            ("POST", r"/_matrix/client/v3/rooms/{rid}/invite"),
            ("POST", r"/_matrix/client/v3/rooms/{rid}/leave"),
            ("POST", r"/_matrix/client/v3/join/{rid}"),
            ("GET", r"/_matrix/client/v1/rooms/{rid}/hierarchy"),
            ("DELETE", r"/_synapse/admin/v1/rooms/{rid}"),
            ("GET", r"/_synapse/admin/v1/rooms/{rid}"),
        ]:
            app.router.add_route(method, path, self._handler)
        self.app = app

    async def _handler(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "query": dict(request.query),
                "auth": request.headers.get("Authorization", ""),
                "body": body,
            }
        )
        return self._respond(request)

    def _respond(self, request: web.Request) -> web.Response:
        if request.path.endswith("/createRoom"):
            return web.json_response({"room_id": ROOM_ID})
        if "/send/m.room.message" in request.path:
            return web.json_response({"event_id": EVENT_ID})
        if request.method == "GET" and "/state/" in request.path:
            return web.json_response(BASE_PL)
        if request.method == "PUT" and "/state/" in request.path:
            return web.json_response({"event_id": "$state1"})
        if request.path.endswith("/hierarchy"):
            return web.json_response({"rooms": [{"room_id": ROOM_ID, "room_type": "m.space"}]})
        if request.path.endswith("/register"):
            return web.json_response({"user_id": AGENT})
        if request.method == "DELETE" and "/_synapse/admin/" in request.path:
            return web.json_response({"kicked_users": [], "local_aliases": [], "new_room_id": None})
        if request.method == "GET" and "/_synapse/admin/" in request.path:
            return web.json_response({"room_id": ROOM_ID, "name": "x"})
        return web.json_response({})

    def last(self) -> dict:
        return self.requests[-1]

    def find(self, method: str, path_suffix: str) -> dict:
        for req in self.requests:
            if req["method"] == method and req["path"].endswith(path_suffix):
                return req
        raise AssertionError(f"no {method} {path_suffix} request; got {self.requests}")


@pytest_asyncio.fixture
async def mock():
    server = MockHomeserver()
    async with TestClient(TestServer(server.app)) as tc:
        yield server, tc


@pytest_asyncio.fixture
async def client(mock):
    _server, tc = mock
    async with MatrixClient(
        tc.server.app_root if False else str(tc.make_url("")),
        AS_TOKEN,
        server_name=SERVER,
        admin_token=ADMIN_TOKEN,
    ) as c:
        yield c


# --- masquerade + auth (the D3 contract) ---------------------------------------


class TestMasqueradeAndAuth:
    @pytest.mark.asyncio
    async def test_client_api_masquerades_user_id_param(self, client, mock):
        server, _ = mock
        await client.send_message(ROOM_ID, "hi", sender=AGENT)
        # txnId-suffixed PUT path (idempotent send): match by infix.
        req = next(
            r for r in server.requests
            if r["method"] == "PUT" and "/send/m.room.message/" in r["path"]
        )
        assert req["query"]["user_id"] == AGENT
        assert req["auth"] == f"Bearer {AS_TOKEN}"

    @pytest.mark.asyncio
    async def test_client_api_without_sender_omits_user_id(self, client, mock):
        server, _ = mock
        await client.join_room(ROOM_ID, sender=AGENT)  # ensure session warm
        server.requests.clear()
        await client.get_power_levels(ROOM_ID, sender=None)
        req = server.find("GET", "/state/m.room.power_levels/")
        assert "user_id" not in req["query"]

    @pytest.mark.asyncio
    async def test_admin_api_uses_owner_token(self, client, mock):
        server, _ = mock
        await client.delete_room(ROOM_ID)
        req = server.find("DELETE", f"/rooms/{ROOM_ID}")
        assert req["path"] == f"/_synapse/admin/v1/rooms/{ROOM_ID}"
        assert req["auth"] == f"Bearer {ADMIN_TOKEN}"

    @pytest.mark.asyncio
    async def test_admin_api_requires_admin_token(self, mock):
        _server, tc = mock
        async with MatrixClient(str(tc.make_url("")), AS_TOKEN) as c:
            with pytest.raises(MatrixError) as exc:
                await c.delete_room(ROOM_ID)
            assert exc.value.errcode == "M_NO_ADMIN_TOKEN"


# --- rooms ------------------------------------------------------------------------


class TestCreateRoom:
    @pytest.mark.asyncio
    async def test_space_creation_content_and_preset(self, client, mock):
        server, _ = mock
        rid = await client.create_room(
            name="Orchestrator", sender=AGENT, invite=(OWNER,), space=True
        )
        assert rid == ROOM_ID
        req = server.find("POST", "/createRoom")
        assert req["query"]["user_id"] == AGENT
        assert req["body"]["creation_content"] == {"type": "m.space"}
        assert req["body"]["preset"] == "private_chat"
        assert req["body"]["invite"] == [OWNER]
        assert req["body"]["name"] == "Orchestrator"

    @pytest.mark.asyncio
    async def test_chat_room_trusted_private_no_creation_content(self, client, mock):
        server, _ = mock
        await client.create_room(name="chat", sender=AGENT)
        body = server.find("POST", "/createRoom")["body"]
        assert body["preset"] == "trusted_private_chat"
        assert "creation_content" not in body


# --- messages -----------------------------------------------------------------------


class TestMessages:
    @pytest.mark.asyncio
    async def test_send_message_text_and_formatted(self, client, mock):
        server, _ = mock
        event = await client.send_message(
            ROOM_ID, "plain", sender=AGENT, formatted_body="<em>plain</em>"
        )
        assert event == EVENT_ID
        body = next(
            r for r in server.requests
            if r["method"] == "PUT" and "/send/m.room.message/" in r["path"]
        )["body"]
        assert body["msgtype"] == "m.text"
        assert body["body"] == "plain"
        assert body["format"] == "org.matrix.custom.html"
        assert body["formatted_body"] == "<em>plain</em>"

    @pytest.mark.asyncio
    async def test_send_message_without_format(self, client, mock):
        server, _ = mock
        await client.send_message(ROOM_ID, "plain", sender=AGENT)
        body = next(
            r for r in server.requests
            if r["method"] == "PUT" and "/send/m.room.message/" in r["path"]
        )["body"]
        assert "format" not in body
        assert "formatted_body" not in body

    @pytest.mark.asyncio
    async def test_edit_message_replace_shape(self, client, mock):
        server, _ = mock
        await client.edit_message(
            ROOM_ID, "$orig", "rev2", sender=AGENT, formatted_body="<p>rev2</p>"
        )
        body = next(
            r for r in server.requests
            if r["method"] == "PUT" and "/send/m.room.message/" in r["path"]
        )["body"]
        assert body["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$orig"}
        assert body["m.new_content"]["body"] == "rev2"
        assert body["m.new_content"]["formatted_body"] == "<p>rev2</p>"
        assert body["body"].startswith("* ")


# --- state ------------------------------------------------------------------------------


class TestStateEvents:
    @pytest.mark.asyncio
    async def test_set_space_child_via_server(self, client, mock):
        server, _ = mock
        await client.set_space_child("!sp:a", "!ch:a", sender=AGENT, via=(SERVER,))
        req = server.find(
            "PUT", "/state/m.space.child/!ch:a"
        )
        assert req["path"].startswith("/_matrix/client/v3/rooms/!sp:a/state/")
        assert req["body"] == {"via": [SERVER]}
        assert req["query"]["user_id"] == AGENT

    @pytest.mark.asyncio
    async def test_set_space_child_remove_sends_empty_content(self, client, mock):
        server, _ = mock
        await client.set_space_child("!sp:a", "!ch:a", sender=AGENT, remove=True)
        assert server.find("PUT", "/state/m.space.child/!ch:a")["body"] == {}

    @pytest.mark.asyncio
    async def test_set_power_levels_read_modify_write(self, client, mock):
        server, _ = mock
        await client.set_power_levels(ROOM_ID, {OWNER: 100}, sender=AGENT)
        put = next(
            r for r in server.requests if r["method"] == "PUT" and "power_levels" in r["path"]
        )
        get = next(
            r for r in server.requests if r["method"] == "GET" and "power_levels" in r["path"]
        )
        assert get["path"].endswith("/state/m.room.power_levels/")
        # merged: base PL content kept, owner added at 100
        assert put["body"]["users"][AGENT] == 100
        assert put["body"]["users"][OWNER] == 100
        assert put["body"]["events"] == BASE_PL["events"]
        assert put["body"]["state_default"] == 50

    @pytest.mark.asyncio
    async def test_membership_paths(self, client, mock):
        server, _ = mock
        await client.invite(ROOM_ID, OWNER, sender=AGENT)
        assert server.find("POST", f"/rooms/{ROOM_ID}/invite")["body"] == {"user_id": OWNER}
        await client.join_room(ROOM_ID, sender=AGENT)
        assert any(r["path"] == f"/_matrix/client/v3/join/{ROOM_ID}" for r in server.requests)
        await client.leave_room(ROOM_ID, sender=AGENT)
        assert any(
            r["method"] == "POST" and r["path"] == f"/_matrix/client/v3/rooms/{ROOM_ID}/leave"
            for r in server.requests
        )


class TestOwnerAutoJoin:
    """Owner auto-join (VM round 2): the sidecar accepts the owner's own
    invites with the owner's own credential — the same POST /join a Join
    tap sends. join_rule stays invite for everyone else."""

    @pytest.mark.asyncio
    async def test_join_room_as_owner_uses_owner_token(self, client, mock):
        server, _ = mock
        rid = await client.join_room_as_owner(ROOM_ID)
        req = server.find("POST", f"/join/{ROOM_ID}")
        assert req["auth"] == f"Bearer {ADMIN_TOKEN}"
        assert "user_id" not in req["query"]  # owner credential, not masquerade
        assert rid == ROOM_ID

    @pytest.mark.asyncio
    async def test_join_room_as_owner_requires_owner_token(self, mock):
        _server, tc = mock
        async with MatrixClient(str(tc.make_url("")), AS_TOKEN) as c:
            with pytest.raises(MatrixError) as exc:
                await c.join_room_as_owner(ROOM_ID)
            assert exc.value.errcode == "M_NO_ADMIN_TOKEN"


# --- reads ------------------------------------------------------------------------------


class TestReads:
    @pytest.mark.asyncio
    async def test_hierarchy_get(self, client, mock):
        server, _ = mock
        out = await client.room_hierarchy(ROOM_ID, sender=AGENT)
        req = server.find("GET", "/hierarchy")
        assert req["path"] == f"/_matrix/client/v1/rooms/{ROOM_ID}/hierarchy"
        assert req["query"]["suggested_only"] == "false"
        assert out["rooms"][0]["room_type"] == "m.space"

    @pytest.mark.asyncio
    async def test_register_virtual_user(self, client, mock):
        server, _ = mock
        out = await client.register_virtual_user("merc_auth-refactor")
        assert out == AGENT
        req = server.find("POST", "/register")
        assert req["auth"] == f"Bearer {AS_TOKEN}"
        assert req["body"]["type"] == "m.login.application_service"


# --- admin purge ----------------------------------------------------------------------------


class TestAdminPurge:
    @pytest.mark.asyncio
    async def test_delete_room_block_purge_body(self, client, mock):
        server, _ = mock
        out = await client.delete_room(ROOM_ID)
        req = server.find("DELETE", f"/rooms/{ROOM_ID}")
        assert req["method"] == "DELETE"
        assert req["body"] == {"block": False, "purge": True}
        assert out["kicked_users"] == []

    @pytest.mark.asyncio
    async def test_room_alive_404_false(self):
        # Dedicated app: the shared mock's router is frozen once served,
        # so the 404 shape gets its own server.
        async def _handler(request: web.Request) -> web.Response:
            if request.path.endswith("/!gone:x"):
                return web.json_response(
                    {"errcode": "M_NOT_FOUND", "error": "not found"}, status=404
                )
            return web.json_response({"room_id": ROOM_ID, "name": "x"})

        app = web.Application()
        app.router.add_get(r"/_synapse/admin/v1/rooms/{rid}", _handler)
        async with TestClient(TestServer(app)) as tc:
            async with MatrixClient(
                str(tc.make_url("")), AS_TOKEN, admin_token=ADMIN_TOKEN
            ) as c:
                assert await c.admin_room_alive("!gone:x") is False
                assert await c.admin_room_alive(ROOM_ID) is True


class TestErrors:
    @pytest.mark.asyncio
    async def test_matrix_error_carries_status_and_errcode(self):
        # Dedicated app (see above): no mutating the shared frozen router.
        async def _forbidden(request: web.Request) -> web.Response:
            await request.read()
            return web.json_response(
                {"errcode": "M_FORBIDDEN", "error": "nope"}, status=403
            )

        app = web.Application()
        app.router.add_post(r"/_matrix/client/v3/forbidden", _forbidden)
        async with TestClient(TestServer(app)) as tc:
            async with MatrixClient(str(tc.make_url("")), AS_TOKEN) as c:
                with pytest.raises(MatrixError) as exc:
                    await c.client_api("POST", "/_matrix/client/v3/forbidden")
                assert exc.value.status == 403
                assert exc.value.errcode == "M_FORBIDDEN"


class TestFromRegistration:
    def test_loads_as_token_from_yaml(self, tmp_path):
        reg = tmp_path / "merc-observatory.yaml"
        reg.write_text(
            "id: merc-observatory\n"
            "url: http://127.0.0.1:18090\n"
            f"as_token: {AS_TOKEN}\n"
            "hs_token: deadbeef\n"
            "namespaces:\n  users:\n    - regex: '^@merc_.*$'\n"
            "      exclusive: true\n",
            encoding="utf-8",
        )
        c = MatrixClient.from_registration(reg, "http://127.0.0.1:18008")
        assert c.as_token == AS_TOKEN
        assert c.homeserver_url == "http://127.0.0.1:18008"


    def _flaky_client(self, tc, hook):
        return MatrixClient(str(tc.make_url("")), AS_TOKEN,
                            server_name=SERVER, admin_token="stale-tok",
                            on_admin_401=hook)

    @pytest.mark.asyncio
    async def test_purge_401_refreshes_and_retries(self):
        seen: list[str] = []

        async def _admin(request: web.Request) -> web.Response:
            seen.append(request.headers.get("Authorization", ""))
            if len(seen) == 1:
                return web.json_response(
                    {"errcode": "M_UNKNOWN_TOKEN", "error": "stale"},
                    status=401)
            return web.json_response({"kicked_users": []})

        async def _hook():
            return "fresh-tok"

        app = web.Application()
        app.router.add_route("DELETE", r"/_synapse/admin/v1/rooms/{rid}", _admin)
        async with TestClient(TestServer(app)) as tc:
            c = await self._flaky_client(tc, _hook).__aenter__()
            try:
                out = await c.delete_room("!r:x")
            finally:
                await c.__aexit__()
            assert out == {"kicked_users": []}
            assert seen == ["Bearer stale-tok", "Bearer fresh-tok"]
            assert c.admin_token == "fresh-tok"

    @pytest.mark.asyncio
    async def test_purge_401_without_hook_raises(self):
        async def _admin(request: web.Request) -> web.Response:
            return web.json_response(
                {"errcode": "M_UNKNOWN_TOKEN", "error": "stale"}, status=401)

        app = web.Application()
        app.router.add_route("DELETE", r"/_synapse/admin/v1/rooms/{rid}", _admin)
        async with TestClient(TestServer(app)) as tc:
            async with MatrixClient(str(tc.make_url("")), AS_TOKEN,
                                    admin_token="stale-tok") as c:
                with pytest.raises(MatrixError) as exc:
                    await c.delete_room("!r:x")
                assert exc.value.status == 401

    @pytest.mark.asyncio
    async def test_purge_401_failed_refresh_raises_original(self):
        async def _admin(request: web.Request) -> web.Response:
            return web.json_response(
                {"errcode": "M_UNKNOWN_TOKEN", "error": "stale"}, status=401)

        async def _hook():
            return None

        app = web.Application()
        app.router.add_route("DELETE", r"/_synapse/admin/v1/rooms/{rid}", _admin)
        async with TestClient(TestServer(app)) as tc:
            c = await self._flaky_client(tc, _hook).__aenter__()
            try:
                with pytest.raises(MatrixError) as exc:
                    await c.delete_room("!r:x")
                assert exc.value.status == 401
            finally:
                await c.__aexit__()
