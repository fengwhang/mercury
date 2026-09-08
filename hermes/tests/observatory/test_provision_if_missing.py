"""provision_if_missing — the first-time provision gate for `mercury update`.

Existing installs that predate the observatory have no tuwunel.version
file; `refresh_for_update` alone only swaps the binary and never creates
toml/appservice/owner/unit. The gate must:

* provision (exactly once) when the version file is missing and the
  observatory is enabled (config default governs — default ON);
* skip silently when the version file already exists;
* skip silently when the observatory is disabled in config;
* skip silently when offline (config or explicit) with nothing installed —
  provisioning would hard-fail with no network and nothing to trust;
* let TuwunelError/ProvisionError PROPAGATE (the update tail warns, it
  never blocks — but the gate itself must not swallow failures).

Network and provision() itself are mocked throughout.
"""

from __future__ import annotations

import pytest

import observatory.provision as prov


class _Probe:
    """Records every provision() call."""

    def __init__(self, *, result=None, raises: Exception | None = None):
        self.calls: list[dict] = []
        self._result = result if result is not None else {"tuwunel": {}, "ok": True}
        self._raises = raises

    def __call__(self, mercury_home=None, registration_token=None, **kw):
        self.calls.append({"mercury_home": mercury_home, **kw})
        if self._raises is not None:
            raise self._raises
        return self._result


def _mark_version_present(tmp_path) -> None:
    vf = tmp_path / "observatory" / "bin" / "tuwunel.version"
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text("1.9.0\n", encoding="utf-8")


@pytest.fixture
def gates(monkeypatch):
    """Config gates under test control (default: enabled, online)."""
    state = {"enabled": True, "offline": False}
    monkeypatch.setattr(prov, "observatory_enabled", lambda: state["enabled"])
    monkeypatch.setattr(prov, "observatory_offline", lambda: state["offline"])
    return state


def test_missing_provisions(tmp_path, monkeypatch, gates):
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    summary = prov.provision_if_missing(tmp_path)
    assert summary is probe._result
    assert len(probe.calls) == 1
    assert probe.calls[0]["mercury_home"] == tmp_path
    assert probe.calls[0]["offline"] is False  # online path only


def test_present_skips(tmp_path, monkeypatch, gates):
    _mark_version_present(tmp_path)
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    assert prov.provision_if_missing(tmp_path) is None
    assert probe.calls == []


def test_disabled_silent(tmp_path, monkeypatch, gates):
    gates["enabled"] = False
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    assert prov.provision_if_missing(tmp_path) is None
    assert probe.calls == []


def test_offline_config_with_missing_silent(tmp_path, monkeypatch, gates):
    gates["offline"] = True
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    assert prov.provision_if_missing(tmp_path) is None
    assert probe.calls == []


def test_offline_explicit_with_missing_silent(tmp_path, monkeypatch, gates):
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    assert prov.provision_if_missing(tmp_path, offline=True) is None
    assert probe.calls == []


def test_offline_with_version_present_still_skips(tmp_path, monkeypatch, gates):
    _mark_version_present(tmp_path)
    gates["offline"] = True
    probe = _Probe()
    monkeypatch.setattr(prov, "provision", probe)
    assert prov.provision_if_missing(tmp_path) is None
    assert probe.calls == []


def test_failure_propagates(tmp_path, monkeypatch, gates):
    boom = prov.ProvisionError("github unreachable")
    monkeypatch.setattr(prov, "provision", _Probe(raises=boom))
    with pytest.raises(prov.ProvisionError, match="github unreachable"):
        prov.provision_if_missing(tmp_path)
