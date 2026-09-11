"""Verify-notice contract: refused/pending only, gateway room only,
client-agnostic copy with the trust-device command, per-room_key dedupe."""
from __future__ import annotations

import pytest

from observatory.e2ee import E2EEManager, trust_device_command
from observatory.state import ObservatoryState

GW = "@merc_gateway:hs"
OWNER = "@owner:hs"
GW_ROOM = "!gwroom:hs"
DIRECTIVES_ROOM = "!directives:hs"
AGENT_ROOM = "!agentroom:hs"


class _FakeAccount:
    fingerprint = "AB12 CD34 EF56"


class _FakeInner:
    account = _FakeAccount()


class _FakeVirtual:
    def __init__(self, mxid: str = GW, device_id: str = "GWDEV1") -> None:
        self.mxid = mxid
        self.device_id = device_id
        self.machine = _FakeInner()

    async def load(self) -> None:
        return None


def _manager(tmp_path, *, seed_gateway: bool = True):
    state = ObservatoryState(tmp_path / "state.db")
    if seed_gateway:
        state.add_node(
            "gw", engine="hermes", name="gateway agent", slug="gateway-agent",
            mxid=GW, session_ref="session:gateway", parent_node_id=None,
            extra={"kind": "gateway"},
        )
        state.set_room_id("gw", GW_ROOM)
    mgr = E2EEManager(
        object(), state, crypto_dir=tmp_path / "crypto",
        owner_mxid=OWNER, gateway_mxid=GW)
    mgr._machines[GW] = _FakeVirtual()
    sent: list[tuple[str, str, str]] = []

    async def fake_send(room_id, *, sender, body, **_kwargs) -> str:
        sent.append((room_id, sender, body))
        return "$notice1"

    mgr.send_encrypted_message = fake_send  # type: ignore[method-assign]
    return mgr, sent


@pytest.mark.asyncio
async def test_trust_only_never_posts(tmp_path):
    mgr, sent = _manager(tmp_path)
    report = {"trusted": ["PHONE1"], "known": [], "refused": [],
              "pending": [], "fetched": ["PHONE1"], "shared": []}
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report) is False
    assert sent == []


@pytest.mark.asyncio
async def test_refused_posts_to_gateway_room_only(tmp_path):
    mgr, sent = _manager(tmp_path)
    report = {"trusted": [], "known": [], "refused": ["PHONE1"],
              "pending": [], "fetched": ["PHONE1"], "shared": []}
    # triggered from the directives room — notice must land in the gateway room
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report) is True
    assert len(sent) == 1
    room_id, sender, body = sent[0]
    assert room_id == GW_ROOM
    assert room_id != DIRECTIVES_ROOM
    assert sender == GW
    assert "PHONE1" in body
    # triggered from an agent room — still the gateway room, never the agent room
    assert await mgr.maybe_post_verify_notice(
        AGENT_ROOM, sender=GW, room_key="some-agent",
        report=report) is True
    assert len(sent) == 2
    assert sent[1][0] == GW_ROOM
    assert sent[1][0] != AGENT_ROOM


@pytest.mark.asyncio
async def test_pending_posts_to_gateway_room(tmp_path):
    mgr, sent = _manager(tmp_path)
    report = {"trusted": [], "known": [], "refused": ["PHONE1"],
              "pending": ["PHONE1"], "fetched": ["PHONE1"], "shared": []}
    assert await mgr.maybe_post_verify_notice(
        AGENT_ROOM, sender=GW, room_key="some-agent",
        report=report) is True
    assert len(sent) == 1
    assert sent[0][0] == GW_ROOM
    assert trust_device_command("PHONE1") in sent[0][2]


@pytest.mark.asyncio
async def test_skips_when_gateway_room_unknown(tmp_path):
    mgr, sent = _manager(tmp_path, seed_gateway=False)
    report = {"trusted": [], "known": [], "refused": ["PHONE1"],
              "pending": ["PHONE1"], "fetched": ["PHONE1"], "shared": []}
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report) is False
    assert sent == []


def test_copy_has_command_and_no_client_steps(tmp_path):
    mgr, _sent = _manager(tmp_path)
    text = mgr.verify_notice_text(
        gateway_mxid=GW, device_id="GWDEV1", fingerprint="AB12 CD34",
        trusted=[], refused=["PHONE1"], pending=["PHONE1"])
    assert trust_device_command("PHONE1") in text
    assert "AB12 CD34" in text
    assert "device details screen" in text
    for banned in ("FluffyChat", "Element", "Tap ", " tap ", "SAS", "emoji"):
        assert banned not in text, banned


def test_copy_trusted_states_what_happened(tmp_path):
    mgr, _sent = _manager(tmp_path)
    text = mgr.verify_notice_text(
        gateway_mxid=GW, device_id="GWDEV1", fingerprint="AB12 CD34",
        trusted=["PHONE1"], refused=["TABLET2"], pending=[])
    assert "PHONE1" in text and "TABLET2" in text
    assert trust_device_command("TABLET2") in text
    for banned in ("FluffyChat", "Element"):
        assert banned not in text, banned


@pytest.mark.asyncio
async def test_dedupe_per_room_key_intact(tmp_path):
    mgr, sent = _manager(tmp_path)
    report = {"trusted": [], "known": [], "refused": ["PHONE1"],
              "pending": [], "fetched": ["PHONE1"], "shared": []}
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report) is True
    assert len(sent) == 1
    # same picture, same key → no repost
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report) is False
    assert len(sent) == 1
    # changed picture (second refusal) → repost
    report2 = {"trusted": [], "known": [], "refused": ["PHONE1", "TABLET2"],
               "pending": [], "fetched": ["PHONE1", "TABLET2"], "shared": []}
    assert await mgr.maybe_post_verify_notice(
        DIRECTIVES_ROOM, sender=GW, room_key="directives",
        report=report2) is True
    assert len(sent) == 2 and "TABLET2" in sent[1][2]
    # same new picture under a different key posts separately (per-key dedupe)
    assert await mgr.maybe_post_verify_notice(
        AGENT_ROOM, sender=GW, room_key="other-key",
        report=report2) is True
    assert len(sent) == 3
