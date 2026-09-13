"""Matrix SteerText routing is content-agnostic (zero new commands).

Room plain text — `stop`, `apple`, anything — routes as SteerText, never as
a sidecar verb. Both shapes must yield the same InjectText steer disposition
for the gateway room: no content-specific branching, no new verbs.
"""
from __future__ import annotations

import pytest

from observatory.control import (
    AgentClass,
    ControlRouter,
    InjectText,
    PowerLevelSnapshot,
    RoomPowerLevels,
    parse_intent,
    SteerText,
)
from observatory.identity import assign_slug, virtual_mxid
from observatory.state import ObservatoryState

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"
GW = "gw"


def room_of(node_id: str) -> str:
    return f"!room-{node_id}:{SERVER}"


def seed_state(tmp_path):
    state = ObservatoryState(tmp_path / "state.db")
    slug = assign_slug("gateway agent", state)
    state.add_node(
        GW,
        engine="hermes",
        name="gateway agent",
        slug=slug,
        mxid=virtual_mxid(slug),
        session_ref=f"session:{GW}",
        parent_node_id=None,
        extra={"kind": "gateway"},
    )
    state.set_room_id(GW, room_of(GW))
    return state


def make_pl():
    return PowerLevelSnapshot({room_of(GW): RoomPowerLevels(users={OWNER: 100})})


def msg(node_id: str, body: str, *, sender: str = OWNER):
    return {
        "type": "m.room.message",
        "room_id": room_of(node_id),
        "sender": sender,
        "event_id": "$e1",
        "content": {"body": body, "msgtype": "m.text"},
    }


class TestSteerTextContentAgnostic:
    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_plain_text_parses_as_steer_not_verb(self, text):
        intent = parse_intent(text)
        assert isinstance(intent, SteerText)

    @pytest.mark.parametrize("text", ["stop", "apple"])
    def test_gateway_plain_text_routes_as_steer_inject(self, tmp_path, text):
        state = seed_state(tmp_path)
        router = ControlRouter(state, gateway_node_id=GW, pl_provider=make_pl())
        outcome = router.route(msg(GW, text))
        assert outcome.disposition == "steer"
        assert len(outcome.actions) == 1
        action = outcome.actions[0]
        assert isinstance(action, InjectText)
        assert action.text == text
        assert router.agent_class_of(outcome.node_id) is AgentClass.GATEWAY
