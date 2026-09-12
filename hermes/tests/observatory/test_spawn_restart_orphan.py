"""Spawn durability gap (restart orphan): never-materialized 0-agents.

A /spawn whose first turn never completes holds its only handle in the
gateway's in-memory registry. A restart (VM reboot, gateway update) drops
that handle; the lazy session (omp JSONL / hermes SessionDB row) was never
written, so the D18 respawn pass correctly refuses to resume — and the
room sits as a silent live-empty room (reported: orch-3f358091 `Wario`,
omp, session file missing, `session is unavailable` only on message).

Chosen option O3 (boot orphan-marking): boot visibly marks handle-less
never-materialized live 0-agents with an explicit room notice + status —
never silent — with a rebuild-or-exit affordance. The next user message
still rebuilds via the existing on-demand fresh-handle path
(`test_spawn_race.py` first-prompt tests); /exit still removes.

Why not O1 (synchronous materialization at spawn): the omp JSONL only
materializes after the first assistant message (lazy law,
`SessionManager.isSessionOnDisk`), so sync materialization needs a full
LLM turn inside the spawn ack (slow, behavior-changing) or a fake empty
file (invalid session, fork risk); a hermes-only row force would be a
half fix for an omp-reported incident.

Why not eager O2 (rebuild engines at boot): on-demand fresh-build already
exists and is green; eager boot rebuild spawns engines per orphan (slow,
model-null still fails) and still needs a visibility fallback — the
missing piece is the notice, not the rebuild.

D18 holds: same session_ref/MXID (no repoint, no fork), liveness
untouched, exited nodes never resurrected (non-live skipped).
"""

from __future__ import annotations

import socket
import uuid
from pathlib import Path

import pytest

import observatory.sidecar_main as sm
from observatory.state import ObservatoryState

from tests.observatory.test_sidecar_main import FakeMatrixClient

SERVER = "mercury.local"
OWNER = "@owner:mercury.local"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    from observatory.provision import ObservatoryPaths

    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (
        paths.root,
        paths.bin_dir,
        paths.db_dir,
        paths.appservices_dir,
        paths.logs_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    paths.toml.write_text(
        '[global]\nserver_name = "mercury.local"\naddress = "127.0.0.1"\n'
        'port = 18008\ndatabase_path = "db"\nappservice_dir = "as"\n'
        "allow_federation = false\nallow_registration = false\n"
        'registration_token = "tok"\n',
        encoding="utf-8",
    )
    paths.appservice_registration.write_text(
        "id: merc-observatory\nurl: http://127.0.0.1:18090\n"
        'as_token: "as-tok"\nhs_token: "hs-tok"\n'
        "sender_localpart: merc-bot\nrate_limited: false\n"
        'namespaces:\n  users:\n    - regex: "^@merc_.*$"\n      exclusive: true\n',
        encoding="utf-8",
    )
    paths.owner_credentials.write_text(
        '{"homeserver_url": "http://127.0.0.1:18008", "user_id": "@owner:mercury.local",'
        ' "password": "pw", "access_token": "admin-tok", "device_id": "DEV"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sm.provision,
        "provision",
        lambda **kwargs: {
            "tuwunel": {
                "action": "current",
                "version": "v1.9.0",
                "binary": "x",
                "offline": True,
            }
        },
    )
    return home


@pytest.fixture()
def daemon(fake_home, monkeypatch):
    d = sm.SidecarDaemon(
        fake_home,
        hermes_db=fake_home / "hermes" / "state.db",
        appservice_port=_free_port(),
        e2ee=False,
    )
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


def _seed_live_orphan(pre: ObservatoryState, *, engine: str, name: str, session_ref: str):
    """Live depth-0 row whose first turn never completed (Wario shape)."""
    from observatory.identity import assign_slug, virtual_mxid
    from observatory.spawn import SESSION_MATERIALIZED_KEY

    slug = assign_slug(name, pre)
    return pre.add_node(
        f"orch-{uuid.uuid4().hex[:8]}",
        engine=engine,
        name=name,
        slug=slug,
        mxid=virtual_mxid(slug, server_name=SERVER),
        session_ref=session_ref,
        parent_node_id=None,
        extra={"model": None, SESSION_MATERIALIZED_KEY: False},
    )


def _room_sends(d: sm.SidecarDaemon, room_id: str) -> list:
    return [c for c in d.client.calls if c[0] == "send" and c[1] == room_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["omp", "hermes"])
async def test_restart_marks_never_materialized_orphan_in_room(
    daemon: sm.SidecarDaemon, engine: str
):
    """Restart must not leave a 0-turn agent as a silent live-empty room:
    boot posts an explicit orphan notice (rebuild-or-exit affordance)."""
    if engine == "omp":
        ref = str(
            daemon.mercury_home / "observatory" / "omp-sessions" / "ghost-wario.jsonl"
        )
        assert not Path(ref).exists()  # Wario shape: lazy file never hit disk
    else:
        ref = f"sess-ghost-never-{uuid.uuid4().hex[:8]}"

    pre = ObservatoryState(daemon.paths.root / "state.db")
    try:
        row = _seed_live_orphan(pre, engine=engine, name=f"wario-{engine}", session_ref=ref)
        node_id, mxid = row["node_id"], row["mxid"]
    finally:
        pre.close()

    await daemon.boot()
    try:
        after = daemon.state.get(node_id)
        # No-second-session invariant: same ref/MXID, no fabricated handle.
        assert after["session_ref"] == ref
        assert after["mxid"] == mxid
        assert after["status"] == "live"
        assert daemon.registry.get(node_id) is None
        room_id = after.get("room_id")
        assert room_id, "orphan has no room after boot converge"
        sends = _room_sends(daemon, room_id)
        assert sends, "silent live-empty room after restart: no message posted"
        assert any(
            "first turn" in c[2] and "/exit" in c[2] for c in sends
        ), f"orphan notice missing rebuild-or-exit affordance: {[c[2] for c in sends]}"
        assert f"child-orphan:{node_id}" in daemon.routing_log
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_boot_orphan_marking_skips_dead_and_materialized(daemon: sm.SidecarDaemon):
    """Exited nodes are never resurrected; materialized-dangling rows keep
    the existing resume-failure path (no orphan notice for either)."""
    pre = ObservatoryState(daemon.paths.root / "state.db")
    try:
        dead = _seed_live_orphan(
            pre,
            engine="omp",
            name="dead-orphan",
            session_ref=str(
                daemon.mercury_home / "observatory" / "omp-sessions" / "ghost-dead.jsonl"
            ),
        )
        pre.mark_dead(dead["node_id"])
        mat = _seed_live_orphan(
            pre,
            engine="omp",
            name="dangling-mat",
            session_ref=str(
                daemon.mercury_home / "observatory" / "omp-sessions" / "ghost-gone.jsonl"
            ),
        )
        pre.update_extra(mat["node_id"], session_materialized=True)
        dead_id, mat_id = dead["node_id"], mat["node_id"]
    finally:
        pre.close()

    await daemon.boot()
    try:
        assert daemon.registry.get(dead_id) is None
        assert daemon.registry.get(mat_id) is None
        assert f"child-orphan:{dead_id}" not in daemon.routing_log
        assert f"child-orphan:{mat_id}" not in daemon.routing_log
        assert not any(
            f"child-orphan:{dead_id}" in entry or f"child-orphan:{mat_id}" in entry
            for entry in daemon.routing_log
        )
    finally:
        await daemon.shutdown()
