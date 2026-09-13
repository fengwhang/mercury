"""Owner-device VERIFIED trust survives restarts via the SQLite mirror.

Live truth (2026-09-12): both VM crypto DBs hold owner devices at trust=0 —
nothing ever reaches VERIFIED=2 — because mautrix ``_validate_device``
resets every refetched device to UNVERIFIED while our reads hit the memory
dict and the known-device path never re-persists. Genuine rotations (the
A2SrZ7yScF identity reset) must stay refused-but-surfaced: the only path
back is operator approval via ``trust-device``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from observatory.e2ee import E2EEManager, e2ee_available
from observatory.state import ObservatoryState

needs_stack = pytest.mark.skipif(not e2ee_available(), reason="compiled olm stack missing")

GW = "@merc_gateway:hs"
OWNER = "@owner:hs"
PHONE = "PHONE1"


class _StoreVirtual:
    """machine_for-shaped shim: only ``load`` + ``.machine.crypto_store``."""

    def __init__(self, mxid: str, store) -> None:
        self.mxid = mxid
        self.machine = SimpleNamespace(crypto_store=store)

    async def load(self) -> None:
        return None


def _device(name, identity_key, signing_key, trust=None):
    from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

    return DeviceIdentity(
        user_id=UserID(OWNER), device_id=DeviceID(name),
        identity_key=identity_key, signing_key=signing_key,
        trust=TrustState.UNVERIFIED if trust is None else trust,
        deleted=False, name=name)


async def _rig(tmp_path, db_name="merc_gateway.db"):
    from observatory.e2ee import SQLiteCryptoStore

    db = tmp_path / "crypto" / db_name
    store = SQLiteCryptoStore(db, GW)
    await store.open()
    state = ObservatoryState(tmp_path / "state.db")
    mgr = E2EEManager(
        SimpleNamespace(), state, crypto_dir=tmp_path / "crypto",
        owner_mxid=OWNER, gateway_mxid=GW)
    mgr._machines[GW] = _StoreVirtual(GW, store)
    return mgr, store, db


@needs_stack
class TestTrustPersist:
    @pytest.mark.asyncio
    async def test_verify_survives_store_reload(self, tmp_path):
        """(a) verify, kill the store object, reopen: still VERIFIED."""
        from mautrix.types import TrustState

        from observatory.e2ee import SQLiteCryptoStore

        mgr, store, db = await _rig(tmp_path)
        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-a", "ed-a")) is True
        await store.close()

        reopened = SQLiteCryptoStore(db, GW)
        await reopened.open()
        try:
            got = await reopened.get_device(OWNER, PHONE)
            assert got is not None and got.trust == TrustState.VERIFIED
            assert str(got.identity_key) == "curve-a"
        finally:
            await reopened.close()

    @pytest.mark.asyncio
    async def test_memory_sqlite_disagreement_decides_from_durable(self, tmp_path):
        """(b) memory empty but SQLite holds a VERIFIED row: a rotated key
        presentation must still REFUSE (never auto-trust), and the durable
        row stays untouched."""
        from mautrix.types import TrustState

        mgr, store, _db = await _rig(tmp_path)
        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-old", "ed-old")) is True
        store._devices.clear()
        assert not store._devices.get(OWNER)  # memory view: empty

        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-NEW", "ed-NEW")) is False

        durably = await store.get_device(OWNER, PHONE)
        assert durably is not None and durably.trust == TrustState.VERIFIED
        assert str(durably.identity_key) == "curve-old"
        await store.close()

    @pytest.mark.asyncio
    async def test_genuine_key_change_never_auto_trusted(self, tmp_path):
        """(c) A2SrZ7yScF shape: same device id, changed keys, memory
        intact — refused, stored trust + keys untouched."""
        from mautrix.types import TrustState

        mgr, store, _db = await _rig(tmp_path)
        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-old", "ed-old")) is True
        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-RESET", "ed-RESET")) is False

        kept = await store.get_device(OWNER, PHONE)
        assert kept is not None and kept.trust == TrustState.VERIFIED
        assert str(kept.identity_key) == "curve-old"
        assert str(kept.signing_key) == "ed-old"
        await store.close()

    @pytest.mark.asyncio
    async def test_known_resight_repairs_fetch_clobber(self, tmp_path):
        """The live bug: mautrix refetch stores known devices UNVERIFIED;
        resighting identical keys must re-persist VERIFIED (memory AND
        SQLite), not return True while leaving trust=0 behind."""
        from mautrix.types import DeviceID, TrustState, UserID

        from observatory.e2ee import SQLiteCryptoStore

        mgr, store, db = await _rig(tmp_path)
        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-a", "ed-a")) is True
        # mautrix _process_fetched_keys replace-set: same keys, UNVERIFIED.
        await store.put_devices(
            UserID(OWNER), {DeviceID(PHONE): _device(PHONE, "curve-a", "ed-a")})
        assert (await store.get_device(OWNER, PHONE)).trust == TrustState.UNVERIFIED

        assert await mgr.trust_device_tofu(
            GW, OWNER, _device(PHONE, "curve-a", "ed-a")) is True
        assert (await store.get_device(OWNER, PHONE)).trust == TrustState.VERIFIED
        await store.close()

        reopened = SQLiteCryptoStore(db, GW)
        await reopened.open()
        try:
            got = await reopened.get_device(OWNER, PHONE)
            assert got is not None and got.trust == TrustState.VERIFIED
        finally:
            await reopened.close()
