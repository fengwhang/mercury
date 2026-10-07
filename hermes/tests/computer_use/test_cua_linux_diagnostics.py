"""Linux diagnostic claims are limited to the calling process."""
import json
from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend, doctor


@pytest.mark.parametrize("display_env", [{"WAYLAND_DISPLAY": "wayland-test"}, {"XDG_SESSION_TYPE": "wayland"}, {}])
def test_doctor_json_identifies_cli_not_gateway_scope(display_env, monkeypatch, capsys):
    for key in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in display_env.items():
        monkeypatch.setenv(key, value)
    report = {"schema_version": "1", "platform": "linux", "overall": "ok", "checks": []}
    with patch.object(cua_backend, "resolve_cua_driver_cmd", return_value="/fake/cua-driver"), \
         patch.object(doctor, "_drive_health_report_or_fallback", return_value=report), \
         patch.object(doctor, "_read_cli_version", return_value="0.30.4"):
        assert doctor.run_doctor(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mercury_environment"] == {"scope": "cli_process", "gateway_environment_checked": False}
    assert payload["overall"] == "ok"


def test_doctor_text_does_not_claim_gateway_checked(capsys):
    report = {"schema_version": "1", "platform": "linux", "overall": "ok", "checks": []}
    with patch.object(cua_backend, "resolve_cua_driver_cmd", return_value="/fake/cua-driver"), \
         patch.object(doctor, "_drive_health_report_or_fallback", return_value=report), \
         patch.object(doctor, "_read_cli_version", return_value="0.30.4"):
        assert doctor.run_doctor(color=False) == 0
    text = capsys.readouterr().out.lower()
    assert "current cli process" in text
    assert "gateway environment was not checked" in text


@pytest.mark.parametrize("manual", [False, True])
def test_native_wayland_empty_discovery_does_not_require_x11(manual, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-test")
    monkeypatch.setenv("CUA_DRIVER_RS_ENABLE_WAYLAND", "1" if manual else "0")
    with patch.object(cua_backend, "_linux_session_locked", return_value=None), \
         patch.object(cua_backend, "_computer_use_cfg", return_value={"native_wayland": not manual}), \
         patch.object(cua_backend.sys, "platform", "linux"):
        reason = cua_backend._empty_discovery_reason().lower()
    assert "no display is set" not in reason
    assert "native wayland" in reason
