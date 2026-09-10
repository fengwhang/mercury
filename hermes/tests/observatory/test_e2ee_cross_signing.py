"""Crash-proof put_cross_signing_key (VM round 2, item 4): mautrix 0.21.1
MemoryCryptoStore.put_cross_signing_key crashes on repeat store — its
else-branch does ``current.key = key`` on the immutable TOFUSigningKey
NamedTuple → AttributeError, which escapes share_group_session, kills
the appservice transaction route, and renders no reply. Our
SQLiteCryptoStore used to call super() FIRST, so the crash hit before
our upsert.

mautrix is site-packages (not vendored — ``pip show mautrix`` =
0.21.1; never edited), so the override is the fix.

These tests run against the REAL mautrix base (importorskip when the
crypto stack is absent): the first test pins the upstream bug shape, the
rest prove the override — double-store same user+usage raises nothing
and the second key wins, in memory AND in the SQLite write-through.
"""
from __future__ import annotations

import pytest

mautrix_store = pytest.importorskip("mautrix.crypto.store")
mautrix_types = pytest.importorskip("mautrix.types")

from observatory import e2ee as e2ee_mod  # noqa: E402


class _FakeDB:
    def __init__(self):
        self.writes: list = []

    async def execute(self, sql, params=()):
        self.writes.append((sql, tuple(params)))

    async def commit(self):
        pass


def _store(tmp_path):
    store = e2ee_mod.SQLiteCryptoStore(tmp_path / "c.db", "@merc_x:mercury.local")
    store._db = _FakeDB()
    return store


@pytest.mark.asyncio
async def test_upstream_repeat_store_crashes():
    """Grounding: the real 0.21.1 base really does crash on repeat store.
    If mautrix ever fixes this, this test red-flags the changed assumption
    (the override then takes the plain path — delete this test)."""
    base = mautrix_store.MemoryCryptoStore("@u:x", "pickle")
    usage = mautrix_types.CrossSigningUsage.MASTER
    key = mautrix_types.SigningKey("ed25519:AAA")
    await base.put_cross_signing_key("@u:x", usage, key)
    with pytest.raises(AttributeError):
        await base.put_cross_signing_key(
            "@u:x", usage, mautrix_types.SigningKey("ed25519:BBB"))


@pytest.mark.asyncio
async def test_double_store_no_crash_second_key_wins(tmp_path):
    """The VM repro: repeat store raises nothing; second key wins in
    memory AND in the SQLite write-through."""
    store = _store(tmp_path)
    usage = mautrix_types.CrossSigningUsage.MASTER
    await store.put_cross_signing_key(
        "@u:x", usage, mautrix_types.SigningKey("ed25519:AAA"))
    await store.put_cross_signing_key(  # used to raise AttributeError
        "@u:x", usage, mautrix_types.SigningKey("ed25519:BBB"))
    assert store._cross_signing_keys["@u:x"][usage].key == "ed25519:BBB"
    assert len(store._db.writes) == 2
    assert store._db.writes[0][1][2] == "ed25519:AAA"
    assert store._db.writes[1][1][2] == "ed25519:BBB"
    assert "ON CONFLICT (user_id, usage)" in store._db.writes[1][0]


@pytest.mark.asyncio
async def test_first_store_unchanged(tmp_path):
    store = _store(tmp_path)
    usage = mautrix_types.CrossSigningUsage.SELF
    await store.put_cross_signing_key(
        "@u:x", usage, mautrix_types.SigningKey("ed25519:AAA"))
    assert store._cross_signing_keys["@u:x"][usage].key == "ed25519:AAA"
    assert len(store._db.writes) == 1


@pytest.mark.asyncio
async def test_unrelated_attribute_error_reraises(tmp_path, monkeypatch):
    """A first store with no entry to repair must not swallow the error."""
    store = _store(tmp_path)

    async def _boom(self, user_id, usage, key):
        raise AttributeError("something else broke")

    monkeypatch.setattr(
        mautrix_store.MemoryCryptoStore, "put_cross_signing_key", _boom)
    with pytest.raises(AttributeError):
        await store.put_cross_signing_key(
            "@u:x", mautrix_types.CrossSigningUsage.MASTER,
            mautrix_types.SigningKey("ed25519:AAA"))
