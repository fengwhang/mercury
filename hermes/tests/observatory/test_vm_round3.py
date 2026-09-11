"""VM round 3: first-login cold start, post-annihilate stale OTK, wipe verify.

Defect 2 — boot converges rooms/spaces + warms power levels BEFORE the
owner has ever joined; the first E2EE share + power snapshot are built
without the owner present. Boot must drop pre-join outbound sessions
when the heal joined anything + refresh power levels AFTER the heal.

Defect 3 — annihilate must verify every target is actually gone (fail
loud on survivors), kill stray tuwunel pids before deleting the DB dir,
and leave a one-shot marker so the NEXT boot force-drops ALL outbound
sessions + re-runs owner trust with no snapshot (first sight).
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import observatory.sidecar_main as sm
from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class FakeMatrixClient:
    homeserver_url: str = "http://127.0.0.1:18008"
    as_token: str = "as-tok"
    server_name: str = "mercury.local"
    admin_token: str = "admin-tok"
    on_admin_401: Any = None
    calls: list = field(default_factory=list)
    next_id: int = 0

    def _id(self, prefix: str) -> str:
        self.next_id += 1
        return f"{prefix}{self.next_id}"

    async def register_virtual_user(self, localpart: str) -> str:
        self.calls.append(("register", localpart))
        return f"@{localpart}:{self.server_name}"

    async def create_room(self, *, name, sender, preset, invite, space=False,
                          topic=None, initial_state=None):
        rid = self._id("!space" if space else "!room")
        self.calls.append(("create_room", name, sender, preset, tuple(invite),
                           space, rid, initial_state))
        return rid

    async def set_power_levels(self, room_id, users, *, sender):
        self.calls.append(("power", room_id, dict(users), sender))

    async def set_space_child(self, space_id, child_id, *, sender, via=(), remove=False):
        self.calls.append(("child", space_id, child_id, sender, tuple(via), remove))

    async def send_message(self, room_id, body, *, sender, formatted_body=None):
        self.calls.append(("send", room_id, body, sender, formatted_body))
        return self._id("$ev")

    async def client_api(self, method, path, *, sender=None, params=None, json_body=None):
        self.calls.append(("client_api", method, path, sender, json_body))
        if "power_levels" in path:
            return {"users": {}, "events_default": 0, "users_default": 0}
        return {"event_id": self._id("$ev")}

    async def room_hierarchy(self, room_id, *, sender, suggested_only=False):
        return {"rooms": []}

    async def close(self) -> None:
        self.calls.append(("close",))

    async def get_power_levels(self, room_id, *, sender):
        self.calls.append(("get_pl", room_id, sender))
        return {"users": {}, "events_default": 0, "users_default": 0}

    async def invite(self, room_id, user_id, *, sender):
        self.calls.append(("invite", room_id, user_id, sender))

    async def join_room(self, room_id, *, sender):
        return room_id

    async def leave_room(self, room_id, *, sender):
        pass

    async def delete_room(self, room_id, **kwargs):
        self.calls.append(("delete", room_id))
        return {}


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "mercury"
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir, paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    paths.toml.write_text(
        "[global]\nserver_name = \"mercury.local\"\naddress = \"127.0.0.1\"\n"
        "port = 18008\ndatabase_path = \"db\"\nappservice_dir = \"as\"\n"
        "allow_federation = false\nallow_registration = false\n"
        "registration_token = \"tok\"\n",
        encoding="utf-8",
    )
    paths.appservice_registration.write_text(
        "id: merc-observatory\nurl: http://127.0.0.1:18090\n"
        "as_token: \"as-tok\"\nhs_token: \"hs-tok\"\n"
        "sender_localpart: merc-bot\nrate_limited: false\n"
        "namespaces:\n  users:\n    - regex: \"^@merc_.*$\"\n      exclusive: true\n",
        encoding="utf-8",
    )
    paths.owner_credentials.write_text(
        '{"homeserver_url": "http://127.0.0.1:18008", "user_id": "@owner:mercury.local",'
        ' "password": "pw", "access_token": "admin-tok", "device_id": "DEV"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sm.provision, "provision",
        lambda **kwargs: {"tuwunel": {"action": "current", "version": "v1.9.0",
                                      "binary": "x", "offline": True}},
    )
    return home


@pytest.fixture()
def daemon(fake_home: Path, monkeypatch) -> sm.SidecarDaemon:
    d = sm.SidecarDaemon(fake_home, hermes_db=fake_home / "hermes" / "state.db",
                         appservice_port=_free_port(), e2ee=False)
    monkeypatch.setattr(d, "_homeserver_healthy", lambda: True)
    monkeypatch.setattr(sm, "MatrixClient", FakeMatrixClient)
    return d


class FakeE2EE:
    """E2EEManager double: records rotation calls, drops nothing real."""

    def __init__(self) -> None:
        self.dropped: list[list[str]] | None = None
        self.post_wipe: list[tuple[list[str], list[str]]] = []

    async def drop_outbound_sessions(self, rooms, senders=()) -> dict[str, int]:
        self.dropped = [list(rooms), list(senders)]
        return {"@merc_gw:mercury.local": len(list(rooms))}

    async def key_query(self, *args, **kwargs) -> dict:
        return {}

    async def key_claim(self, *args, **kwargs) -> dict:
        return {}

    async def stop(self) -> None:
        return None

    async def post_wipe_rotation(self, rooms, senders=()) -> dict[str, Any]:
        self.post_wipe.append((list(rooms), list(senders)))
        return {"dropped": {"@merc_gw:mercury.local": len(list(rooms))},
                "trust": {}, "errors": []}


def _wire_fake_executor(d: sm.SidecarDaemon, monkeypatch, *, joined: int,
                        order: list[str]) -> FakeE2EE:
    """Boot with a fake executor (heal returns ``joined``) + FakeE2EE."""
    fake_e2ee = FakeE2EE()

    async def fake_build_executor():
        from observatory.renderer import IntentExecutor

        d.e2ee = fake_e2ee  # type: ignore[assignment]

        class FakeExec(IntentExecutor):
            async def ensure_owner_in_plan(self, plan):
                order.append("heal")
                return joined

        d.executor = FakeExec(
            d.client, d.state, owner_mxid=d.owner_mxid,
            server_name=d.server_name, gateway_mxid=d.gateway_mxid)
        return d.executor

    monkeypatch.setattr(d, "_build_executor", fake_build_executor)
    orig_pl = d._refresh_power_levels

    async def tracked_pl():
        order.append("pl")
        await orig_pl()

    monkeypatch.setattr(d, "_refresh_power_levels", tracked_pl)
    return fake_e2ee


def _preseed_live_room(home: Path) -> str:
    """One live node with a room id in the boot state.db (pre-join share)."""
    from observatory.state import ObservatoryState

    paths = ObservatoryPaths(home)
    with ObservatoryState(paths.root / "state.db") as st:
        st.add_node("seed0", engine="hermes", name="seed0", slug="seed0",
                    mxid="@merc_seed0:mercury.local", session_ref="session:seed0")
        st.set_room_id("seed0", "!seed:mercury.local")
    return "!seed:mercury.local"


@pytest.mark.asyncio
async def test_boot_rotates_after_heal_and_refreshes_pl_after(
        daemon: sm.SidecarDaemon, fake_home: Path, monkeypatch):
    room = _preseed_live_room(fake_home)
    order: list[str] = []
    fake_e2ee = _wire_fake_executor(daemon, monkeypatch, joined=2, order=order)
    report = await daemon.boot()
    try:
        assert report["owner_joined"] == 2
        # rotation ran for live rooms (pre-seeded room covered)
        assert fake_e2ee.dropped is not None
        assert room in fake_e2ee.dropped[0]
        assert report["megolm_rotated"]
        assert report["megolm_rotated_rooms"] >= 1
        # power refresh ran AFTER the heal, with counts logged
        assert order.index("heal") < order.index("pl")
        assert report["power_rooms"] >= 0
        assert report["directives_members"] >= 0
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_boot_no_heal_no_rotation(daemon: sm.SidecarDaemon, monkeypatch):
    order: list[str] = []
    fake_e2ee = _wire_fake_executor(daemon, monkeypatch, joined=0, order=order)
    report = await daemon.boot()
    try:
        assert report["owner_joined"] == 0
        assert fake_e2ee.dropped is None
        assert report["megolm_rotated"] == {}
        assert "pl" in order  # snapshot still warms, just no rotation
    finally:
        await daemon.shutdown()


@pytest.mark.asyncio
async def test_boot_consumes_post_wipe_marker(
        daemon: sm.SidecarDaemon, fake_home: Path, monkeypatch):
    from observatory.provision import POST_WIPE_MARKER_NAME

    marker = ObservatoryPaths(fake_home).root / POST_WIPE_MARKER_NAME
    marker.write_text("wiped", encoding="utf-8")
    order: list[str] = []
    fake_e2ee = _wire_fake_executor(daemon, monkeypatch, joined=0, order=order)
    report = await daemon.boot()
    try:
        assert fake_e2ee.post_wipe, "marker boot must run post-wipe rotation"
        assert report.get("post_wipe_consumed") is True
        assert not marker.exists(), "marker is one-shot"
    finally:
        await daemon.shutdown()


# --- E2EEManager rotation units (no crypto stack: fake machines) --------------


class FakeStore:
    def __init__(self) -> None:
        self.removed: list[str] = []
        self.devices: dict = {"OLDDEV": object()}

    async def get_outbound_group_session(self, room_id):
        return None if str(room_id) in self.removed else "SESS"

    async def remove_outbound_group_session(self, room_id) -> None:
        self.removed.append(str(room_id))

    async def put_devices(self, user_id, devices) -> None:
        self.devices = dict(devices)


class FakeMachine:
    def __init__(self, store: FakeStore) -> None:
        self.crypto_store = store


class FakeCrypto:
    def __init__(self, store: FakeStore) -> None:
        self.machine = FakeMachine(store)
        self.loaded = False

    async def load(self) -> None:
        self.loaded = True


def _manager(tmp_path: Path):
    from observatory import e2ee as e2ee_mod
    from observatory.state import ObservatoryState

    st = ObservatoryState(tmp_path / "state.db")
    mgr = e2ee_mod.E2EEManager(
        client=object(), state=st, crypto_dir=tmp_path / "crypto",
        owner_mxid="@owner:mercury.local", gateway_mxid="")
    return mgr, st


@pytest.mark.asyncio
async def test_drop_outbound_sessions_skips_owner_and_counts(tmp_path: Path):
    mgr, st = _manager(tmp_path)
    try:
        gw_store, other_store, owner_store = FakeStore(), FakeStore(), FakeStore()
        mgr._machines = {
            "@merc_gw:hs": FakeCrypto(gw_store),
            "@merc_x:hs": FakeCrypto(other_store),
            "@owner:mercury.local": FakeCrypto(owner_store),
        }
        out = await mgr.drop_outbound_sessions(["!r:hs", "!s:hs"])
        assert out == {"@merc_gw:hs": 2, "@merc_x:hs": 2}
        assert gw_store.removed == ["!r:hs", "!s:hs"]
        assert owner_store.removed == [], "owner machine must never rotate"
        # second call: sessions already gone → zero counts, no removal
        out2 = await mgr.drop_outbound_sessions(["!r:hs"])
        assert out2 == {}
    finally:
        st.close()


@pytest.mark.asyncio
async def test_post_wipe_rotation_clears_devices_first_sight(tmp_path: Path):
    mgr, st = _manager(tmp_path)
    try:
        store = FakeStore()
        mgr._machines = {"@merc_gw:hs": FakeCrypto(store)}
        seen: list[str] = []

        async def fake_trust(sender_mxid: str):
            seen.append(sender_mxid)
            return {"trusted": ["NEWDEV"], "known": [], "refused": [],
                    "fetched": ["NEWDEV"], "pending": [], "rotated": []}

        mgr.ensure_owner_trust = fake_trust  # type: ignore[method-assign]
        report = await mgr.post_wipe_rotation(["!r:hs"], ["@merc_gw:hs"])
        assert report["dropped"] == {"@merc_gw:hs": 1}
        assert store.devices == {}, "stored owner devices cleared → first sight"
        assert seen == ["@merc_gw:hs"]
        assert report["trust"]["@merc_gw:hs"]["trusted"] == ["NEWDEV"]
        assert report["errors"] == []
    finally:
        st.close()


# --- wipe verify + marker -----------------------------------------------------


def _seed_wipe_home(home: Path, units: Path) -> ObservatoryPaths:
    paths = ObservatoryPaths(home)
    for d in (paths.root, paths.bin_dir, paths.db_dir, paths.appservices_dir,
              paths.logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    (paths.toml).write_text("[global]\n", encoding="utf-8")
    (paths.db_dir / "tuwunel.db").write_text("data", encoding="utf-8")
    paths.owner_credentials.write_text("{}", encoding="utf-8")
    (paths.appservices_dir / "reg.yaml").write_text("x", encoding="utf-8")
    (paths.root / "state.db").write_text("s", encoding="utf-8")
    (paths.root / "crypto").mkdir(exist_ok=True)
    (home / ".env").write_text("MATRIX_OBS_OWNER_USER_ID=@o:hs\nOTHER=1\n",
                               encoding="utf-8")
    units.mkdir(parents=True, exist_ok=True)
    return paths


def _no_systemd(monkeypatch):
    monkeypatch.setattr(provision_mod, "_systemctl_available", lambda: False)
    monkeypatch.setattr(provision_mod, "_kill_stray_tuwunel", lambda **kw: [])


def test_annihilate_verifies_and_leaves_marker(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "home"
    units = tmp_path / "units"
    paths = _seed_wipe_home(home, units)
    summary = provision_mod.wipe_observatory_data(
        home, mode="annihilate", unit_dir=units)
    assert summary["post_wipe_marker"] == provision_mod.POST_WIPE_MARKER_NAME
    marker = paths.root / provision_mod.POST_WIPE_MARKER_NAME
    assert marker.is_file()
    # marker is invisible to presence checks (fresh reprovision proceeds)
    assert provision_mod.observatory_data_present(home) is False
    assert not (paths.db_dir).exists()


def test_archive_verifies_and_leaves_marker(tmp_path, monkeypatch):
    _no_systemd(monkeypatch)
    home = tmp_path / "home"
    units = tmp_path / "units"
    _seed_wipe_home(home, units)
    summary = provision_mod.wipe_observatory_data(
        home, mode="archive", unit_dir=units)
    assert summary["post_wipe_marker"] == provision_mod.POST_WIPE_MARKER_NAME


def test_wipe_raises_on_surviving_target(tmp_path, monkeypatch):
    """A target the delete did not actually remove fails LOUD (stale-OTK
    shape: the server DB the annihilate never landed)."""
    _no_systemd(monkeypatch)
    home = tmp_path / "home"
    units = tmp_path / "units"
    paths = _seed_wipe_home(home, units)

    real_rmtree = shutil.rmtree

    def flaky_rmtree(target, *args, **kwargs):
        if Path(target) == paths.db_dir:
            return  # simulate a running server / failed delete: survives
        return real_rmtree(target, *args, **kwargs)

    monkeypatch.setattr(provision_mod.shutil, "rmtree", flaky_rmtree)
    with pytest.raises(provision_mod.ProvisionError, match="survived deletion"):
        provision_mod.wipe_observatory_data(home, mode="annihilate", unit_dir=units)
    marker = paths.root / provision_mod.POST_WIPE_MARKER_NAME
    assert not marker.exists(), "half-wipe must leave no marker"


def test_stray_scan_ignores_self_and_nonbinaries():
    """The tuwunel pid scan never matches this pytest process."""
    pids = provision_mod._stray_tuwunel_pids()
    import os

    assert os.getpid() not in pids
