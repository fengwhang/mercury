"""Atomicity contract for SQLiteCryptoStore.put_devices (E2EE store fix).

Live symptom (sidecar.log): EVERY Matrix send dying with
``UNIQUE constraint failed: devices.user_id, devices.device_id`` because
concurrent ``ensure_room_share`` calls interleave the mirror write's
``DELETE`` + multi-row ``INSERT`` statement-by-statement over aiosqlite
(the inherited mautrix ``transaction()`` is a NO-OP).

Contract:
* N concurrent same-user put_devices (same device set) -> zero
  IntegrityError, exactly N rows.
* duplicate device_ids inside ONE call -> single row, no error.
* put_devices(user, {}) still clears the mirror (tracked-empty survives
  a restart — upstream replace semantic for the empty set).
"""
from __future__ import annotations

import asyncio

import pytest

from observatory.e2ee import SQLiteCryptoStore, e2ee_available

needs_stack = pytest.mark.skipif(not e2ee_available(), reason="compiled olm stack missing")


def _dev(user_id, device_id, key="curve-key"):
    from mautrix.types import DeviceID, DeviceIdentity, TrustState, UserID

    return DeviceIdentity(
        user_id=UserID(user_id), device_id=DeviceID(device_id),
        identity_key=f"{key}-{device_id}", signing_key=f"ed-{device_id}",
        trust=TrustState.BLACKLISTED, deleted=False, name="",
    )


def _device_set(user_id, n=3):
    from mautrix.types import DeviceID

    return {DeviceID(f"D{i}"): _dev(user_id, f"D{i}") for i in range(n)}


@needs_stack
class TestPutDevicesAtomic:
    @pytest.mark.asyncio
    async def test_concurrent_same_user_put_devices_no_integrity_error(self, tmp_path):
        from mautrix.types import DeviceID

        db = tmp_path / "crypto" / "atomic.db"
        store = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await store.open()
        try:
            devices = _device_set("@user:hs", 3)
            await asyncio.gather(*(
                store.put_devices("@user:hs", dict(devices)) for _ in range(8)
            ))
            rows = await store._fetchall(
                "SELECT COUNT(*) AS n FROM devices WHERE user_id=?", ("@user:hs",)
            )
            assert rows[0]["n"] == 3
            assert set(await store.get_devices("@user:hs")) == {
                DeviceID("D0"), DeviceID("D1"), DeviceID("D2"),
            }
        finally:
            await store.close()
    @pytest.mark.asyncio
    async def test_duplicate_device_ids_in_one_call_single_row(self, tmp_path):
        class DupMap(dict):
            def __bool__(self):  # empty dict is falsy; `devices or {}` must keep us
                return True

            def items(self):  # same device_id twice — dedupe must collapse
                dev = _dev("@user:hs", "D0")
                return [("D0", dev), ("D0", dev)]

        db = tmp_path / "crypto" / "dup.db"
        store = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await store.open()
        try:
            await store.put_devices("@user:hs", DupMap())
            rows = await store._fetchall(
                "SELECT COUNT(*) AS n FROM devices WHERE user_id=?", ("@user:hs",)
            )
            assert rows[0]["n"] == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_empty_set_clears_mirror_across_restart(self, tmp_path):
        db = tmp_path / "crypto" / "empty.db"
        store = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await store.open()
        await store.put_devices("@user:hs", _device_set("@user:hs", 2))
        await store.put_devices("@user:hs", {})
        await store.close()

        store2 = SQLiteCryptoStore(db, "@merc_gateway:hs")
        await store2.open()
        try:
            assert await store2.get_devices("@user:hs") == {}
        finally:
            await store2.close()
