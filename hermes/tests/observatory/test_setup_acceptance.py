"""Setup acceptance sequence (VM report): ordered gates, card last.

Order under test: dual-bind assert → crypto assert → model resolve +
inject ping → sidecar-encrypted room → admin ping → synthetic
transaction → poison scan (+converge offer) → login card LAST.
Failures report loudly but never block the card.

Fakes carry the provision surface; the environment-dependent helpers
(config load, runtime resolve, ping, synthetic txn) are pinned.
"""
from __future__ import annotations

import mercury_cli.setup as setup_mod
from tests.observatory.test_setup_identity import _provisioned_status
from tests.observatory.test_setup_wizard import (
    _FakeProvision,
    _run_section,
    _write_credentials,
)


def _pin_env(monkeypatch, *, model_cfg=None, provider="acme",
             ping="passed: pong", txn="passed"):
    """Pin the environment-dependent acceptance helpers."""
    monkeypatch.setattr(
        setup_mod, "load_config",
        lambda: {"model": {"default": "acme/widget"}} if model_cfg is None else model_cfg)
    import mercury_cli.runtime_provider as runtime_mod
    monkeypatch.setattr(
        runtime_mod, "resolve_runtime_provider",
        lambda **k: {"provider": provider, "api_key": "k"})
    monkeypatch.setattr(setup_mod, "_inject_ping", lambda *a, **k: ping)
    monkeypatch.setattr(setup_mod, "_synthetic_transaction", lambda: txn)


def _pin_obs(monkeypatch, fake, *, crypto=(True, []), encrypted=True,
             admin="valid", poison=None):
    monkeypatch.setattr(fake, "assert_crypto_stack",
                        lambda: crypto, raising=False)
    monkeypatch.setattr(fake, "gateway_room_encrypted",
                        lambda *a, **k: encrypted, raising=False)
    monkeypatch.setattr(fake, "heal_owner_admin_token",
                        lambda *a, **k: admin, raising=False)
    monkeypatch.setattr(fake, "scan_poisoned_rooms",
                        lambda *a, **k: poison, raising=False)


def _run(monkeypatch, capsys, fake, *, yes_no):
    return _run_section(monkeypatch, capsys, fake, choice=1, yes_no=yes_no)


def test_acceptance_all_pass_then_card_last(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    _pin_obs(monkeypatch, fake, poison=[])
    _pin_env(monkeypatch)
    out, _config, remaining = _run(monkeypatch, capsys, fake, yes_no=[True])
    for gate in ("dual-bind", "crypto", "model", "encrypted-room",
                 "admin", "synthetic-txn", "poison-scan"):
        assert f"[acceptance] {gate}: passed" in out, gate
    assert "acme/widget (acme); inject ping: passed: pong" in out
    assert out.index("[acceptance] poison-scan") < out.index("first login")
    assert "all gates pass or skip" in out
    assert remaining == []


def test_acceptance_model_failure_still_ends_with_card(
        monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    _pin_obs(monkeypatch, fake, poison=[])
    _pin_env(monkeypatch, model_cfg={})
    monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
    out, _config, remaining = _run(monkeypatch, capsys, fake, yes_no=[True])
    assert "[acceptance] model: failed: no model configured" in out
    assert "failures above need action" in out
    assert out.index("[acceptance] model") < out.index("first login")
    assert remaining == []


def test_acceptance_poisoned_offer_reconverges(monkeypatch, capsys, tmp_path):
    creds = _write_credentials(tmp_path)
    fake = _FakeProvision([_provisioned_status(creds)])
    problems = [{"key": "gw", "room_id": "!old:x", "status": "poisoned",
                 "gap_seconds": 3600.0}]
    _pin_obs(monkeypatch, fake, poison=problems)
    fixed: list = []
    monkeypatch.setattr(fake, "reconverge_poisoned_rooms",
                        lambda *a, **k: fixed.append(True) or {
                            "purged": ["!old:x"], "converge": "converged-3"},
                        raising=False)
    _pin_env(monkeypatch)
    out, _config, remaining = _run(
        monkeypatch, capsys, fake, yes_no=[True, True])
    assert "poison-scan: failed: gw (poisoned)" in out
    assert fixed == [True]
    assert "reconverge:" in out
    assert out.index("reconverge:") < out.index("first login")
    assert remaining == []
