"""Contract tests for the Observatory appservice transaction intake
(spec §2, D3). aiohttp test utils; no real homeserver.

Laws under test: token gate (401 on bad/missing as_token), txnId
idempotency (retried transactions dispatch exactly once), queue dispatch
to the handler callback, backpressure (429 = canonical retry signal),
health probe unauthenticated, and the M_NOT_YET_SENT no-consumer stub.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from observatory.appservice import (
    HEALTH_PATH,
    TRANSACTIONS_PATH,
    TransactionIntake,
    as_token_from_registration,
    make_app,
)

TOKEN = "test-as-token"


def _txn_url(txn_id: str) -> str:
    return TRANSACTIONS_PATH.format(txn_id=txn_id)


def _body(*events: dict) -> str:
    return json.dumps({"events": list(events)})


@pytest_asyncio.fixture
async def client():
    """Unauthenticated TestClient over a fresh intake app."""
    intake = TransactionIntake(as_token=TOKEN)
    app = make_app(intake)
    async with TestClient(TestServer(app)) as c:
        yield c


# --- token gate ---------------------------------------------------------------------


class TestTokenGate:
    @pytest.mark.asyncio
    async def test_missing_token_401(self, client: TestClient):
        resp = await client.put(_txn_url("t1"), data=_body())
        assert resp.status == 401
        assert (await resp.json())["errcode"] == "M_UNKNOWN_TOKEN"

    @pytest.mark.asyncio
    async def test_bad_bearer_token_401(self, client: TestClient):
        resp = await client.put(
            _txn_url("t1"), data=_body(), headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status == 401
        assert (await resp.json())["errcode"] == "M_UNKNOWN_TOKEN"

    @pytest.mark.asyncio
    async def test_valid_bearer_accepted(self, client: TestClient):
        resp = await client.put(
            _txn_url("t1"), data=_body(), headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_access_token_query_param_accepted(self, client: TestClient):
        # Homeservers may send the appservice token as ?access_token=
        resp = await client.put(
            f"{_txn_url('t1')}?access_token={TOKEN}", data=_body()
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_needs_no_token(self, client: TestClient):
        resp = await client.get(HEALTH_PATH)
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"


# --- transaction handling -------------------------------------------------------------


class TestTransactions:
    @pytest.mark.asyncio
    async def test_accepted_txn_answers_200(self, client: TestClient):
        resp = await client.put(
            _txn_url("t1"), data=_body({"type": "m.room.message"}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_consumer_stub_is_m_not_yet_sent(self, client: TestClient):
        # M3a skeleton: accepted + queued, nothing forwarded onward yet.
        resp = await client.put(
            _txn_url("t1"), data=_body(), headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status == 200
        assert (await resp.json())["errcode"] == "M_NOT_YET_SENT"

    @pytest.mark.asyncio
    async def test_non_json_body_400(self, client: TestClient):
        resp = await client.put(
            _txn_url("t1"), data="not json",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 400
        assert (await resp.json())["errcode"] == "M_NOT_JSON"

    @pytest.mark.asyncio
    async def test_events_not_a_list_400(self, client: TestClient):
        resp = await client.put(
            _txn_url("t1"), data=json.dumps({"events": {"nope": 1}}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 400
        assert (await resp.json())["errcode"] == "M_BAD_JSON"


class TestDedupAndDispatch:
    @pytest_asyncio.fixture
    async def wired(self):
        """Intake with a recording handler + running consumer."""
        seen: list[tuple[str, list[dict]]] = []

        async def handler(txn_id: str, events: list[dict]) -> None:
            seen.append((txn_id, events))

        intake = TransactionIntake(as_token=TOKEN, handler=handler)
        await intake.start()
        app = make_app(intake)
        async with TestClient(TestServer(app)) as c:
            yield c, intake, seen
        await intake.stop()

    @pytest.mark.asyncio
    async def test_events_reach_handler(self, wired):
        client, _, seen = wired
        events = [{"type": "m.room.message", "event_id": "$1"},
                  {"type": "m.room.member", "event_id": "$2"}]
        resp = await client.put(
            _txn_url("t1"), data=json.dumps({"events": events}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200
        assert (await resp.json()) == {}
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == [("t1", events)]

    @pytest.mark.asyncio
    async def test_retried_txn_id_dispatches_exactly_once(self, wired):
        # The homeserver retries a txn after a network blip even though we
        # answered 200 — dedup by txnId, never re-dispatch.
        client, intake, seen = wired
        for _ in range(3):
            resp = await client.put(
                _txn_url("t42"), data=_body({"event_id": "$x"}),
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert resp.status == 200
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # let any (buggy) second dispatch land
        assert seen == [("t42", [{"event_id": "$x"}])]
        assert not intake.register_txn("t42")  # still remembered as seen

    @pytest.mark.asyncio
    async def test_distinct_txn_ids_both_dispatch(self, wired):
        client, _, seen = wired
        headers = {"Authorization": f"Bearer {TOKEN}"}
        await client.put(_txn_url("a"), data=_body({"n": 1}), headers=headers)
        await client.put(_txn_url("b"), data=_body({"n": 2}), headers=headers)
        for _ in range(100):
            if len(seen) >= 2:
                break
            await asyncio.sleep(0.01)
        assert sorted(txn for txn, _ in seen) == ["a", "b"]

    @pytest.mark.asyncio
    async def test_handler_crash_does_not_kill_intake(self, wired):
        client, intake, seen = wired

        async def boom(txn_id: str, events: list[dict]) -> None:
            raise RuntimeError("consumer bug")

        intake.attach_handler(boom)
        await intake.stop()
        await intake.start()
        resp = await client.put(
            _txn_url("crash"), data=_body({"n": 1}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200
        resp = await client.put(
            _txn_url("after"), data=_body({"n": 2}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200  # intake survived the handler crash
        assert seen == []


class TestBackpressure:
    @pytest.mark.asyncio
    async def test_full_queue_answers_429(self):
        # 429 is the canonical homeserver retry signal — real backpressure,
        # not a silent drop.
        intake = TransactionIntake(as_token=TOKEN, queue_size=1, handler=None)
        app = make_app(intake)
        async with TestClient(TestServer(app)) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            first = await client.put(_txn_url("t1"), data=_body(), headers=headers)
            assert first.status == 200
            second = await client.put(_txn_url("t2"), data=_body(), headers=headers)
            assert second.status == 429
            assert (await second.json())["errcode"] == "M_LIMIT_EXCEEDED"


class TestDedupMemory:
    def test_txn_memory_is_bounded(self):
        intake = TransactionIntake(as_token=TOKEN, txn_memory=3)
        for i in range(5):
            assert intake.register_txn(f"t{i}")
        # Oldest evicted, recent ones still deduped:
        assert intake.register_txn("t0")
        assert intake.register_txn("t1")
        assert not intake.register_txn("t4")

    def test_empty_token_rejected(self):
        with pytest.raises(ValueError):
            TransactionIntake(as_token="")


# --- registration token loader ----------------------------------------------------------


class TestAsTokenLoader:
    def test_reads_as_token_from_registration_yaml(self, tmp_path: Path):
        from observatory.config_gen import render_appservice_registration_yaml

        reg = tmp_path / "merc-observatory.yaml"
        reg.write_text(
            render_appservice_registration_yaml(
                url="http://127.0.0.1:18090",
                as_token="sekrit-as",
                hs_token="sekrit-hs",
            ),
            encoding="utf-8",
        )
        assert as_token_from_registration(reg) == "sekrit-as"

    def test_missing_token_fails_hard(self, tmp_path: Path):
        reg = tmp_path / "bad.yaml"
        reg.write_text("id: x\n", encoding="utf-8")
        with pytest.raises(ValueError, match="as_token"):
            as_token_from_registration(reg)

# --- HS query surface (defect v) ----------------------------------------------------


def _authed(path: str) -> str:
    return f"{path}?access_token={TOKEN}"


@pytest_asyncio.fixture
async def wired():
    """Fresh (client, intake) pair for error-count assertions."""
    from aiohttp.test_utils import TestClient as TC, TestServer as TS

    intake = TransactionIntake(as_token=TOKEN)
    app = make_app(intake)
    async with TC(TS(app)) as c:
        yield c, intake


class TestQuerySurface:
    @pytest.mark.asyncio
    async def test_user_query_ours_answers_200(self, client: TestClient):
        resp = await client.get(_authed("/_matrix/app/v1/users/@merc_gw:x"))
        assert resp.status == 200
        assert await resp.json() == {}

    @pytest.mark.asyncio
    async def test_user_query_foreign_answers_logged_404(self, wired):
        client, intake = wired
        resp = await client.get(_authed("/_matrix/app/v1/users/@alice:other"))
        assert resp.status == 404
        assert (await resp.json())["errcode"] == "M_NOT_FOUND"
        assert intake.error_count() == 1

    @pytest.mark.asyncio
    async def test_alias_query_always_404(self, client: TestClient):
        resp = await client.get(_authed("/_matrix/app/v1/rooms/%23x%3Ay"))
        assert resp.status == 404
        assert (await resp.json())["errcode"] == "M_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_ping_answers_200(self, client: TestClient):
        resp = await client.post(
            _authed("/_matrix/app/v1/ping"),
            data=json.dumps({"transaction_id": "t1"}),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_unknown_route_404_is_logged_and_counted(self, wired):
        client, intake = wired
        resp = await client.get(
            _authed("/_matrix/app/v1/nope"),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status == 404
        assert intake.error_count() == 1

    @pytest.mark.asyncio
    async def test_clean_run_leaves_zero_errors(self, wired):
        """Acceptance hook: no failures across the happy paths."""
        client, intake = wired
        headers = {"Authorization": f"Bearer {TOKEN}"}
        assert (await client.get("/health")).status == 200
        assert (await client.get(
            _authed("/_matrix/app/v1/users/@merc_x:y"),
            headers=headers)).status == 200
        assert (await client.post(
            _authed("/_matrix/app/v1/ping"),
            data="{}", headers=headers)).status == 200
        assert intake.error_count() == 0
