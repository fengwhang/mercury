"""Bound bind URL: owner-credentials.json homeserver_url tracks tuwunel.toml.

VM finding: the credentials file carried http://127.0.0.1:18008 while
tuwunel.toml bound the Tailscale IP (100.86.76.11) — the bind offer
rewrote the toml but never the credentials, misleading consumers. The
sidecar computes its URL from the toml itself, so the file must agree.

Laws: set_tuwunel_bind syncs the credentials URL (Tailscale IP + port,
other keys preserved, 0600 kept); the re-provision 'exists' path heals
a stale URL; status_summary reports the bound URL. No network, no
systemd.
"""

from __future__ import annotations

import json

from observatory import provision as provision_mod
from observatory.config_gen import ObservatoryPaths

TAIL_IP = "100.86.76.11"
STALE_URL = "http://127.0.0.1:18008"
BOUND_URL = f"http://{TAIL_IP}:18008"
MXID = "@owner:mercury.local"
PASSWORD = "s3cr3t-observatory-owner-password"


def _write_home(home, *, address="127.0.0.1", creds_url=STALE_URL):
    obs = home / "observatory"
    obs.mkdir(parents=True, exist_ok=True)
    (obs / "tuwunel.toml").write_text(
        '[global]\nserver_name = "mercury.local"\n'
        f'address = "{address}"\nport = 18008\n',
        encoding="utf-8",
    )
    creds = obs / "owner-credentials.json"
    creds.write_text(
        json.dumps(
            {
                "homeserver_url": creds_url,
                "user_id": MXID,
                "password": PASSWORD,
                "access_token": "tok",
                "device_id": "DEV",
            }
        ),
        encoding="utf-8",
    )
    creds.chmod(0o600)
    return creds


def test_bind_offer_syncs_tailscale_ip_into_credentials(tmp_path):
    home = tmp_path / "mhome"
    creds = _write_home(home)
    assert provision_mod.set_tuwunel_bind(TAIL_IP, home) == TAIL_IP
    doc = json.loads(creds.read_text(encoding="utf-8"))
    assert doc["homeserver_url"] == BOUND_URL
    assert doc["user_id"] == MXID
    assert doc["password"] == PASSWORD
    assert doc["access_token"] == "tok"
    assert oct(creds.stat().st_mode & 0o777) == "0o600"


def test_reprovision_exists_path_heals_stale_credentials_url(tmp_path):
    home = tmp_path / "mhome"
    creds = _write_home(home, address=TAIL_IP, creds_url=STALE_URL)
    assert provision_mod.ensure_owner_account(ObservatoryPaths(home)) == "exists"
    doc = json.loads(creds.read_text(encoding="utf-8"))
    assert doc["homeserver_url"] == BOUND_URL
    assert doc["password"] == PASSWORD


def test_status_summary_reports_bound_url(tmp_path, monkeypatch):
    home = tmp_path / "mhome"
    _write_home(home, address=TAIL_IP)
    monkeypatch.setattr(
        provision_mod, "_homeserver_reachable", lambda url, timeout=2.0: False
    )
    monkeypatch.setattr(provision_mod, "_unit_active", lambda: False)
    st = provision_mod.status_summary(home)
    assert st["homeserver_url"] == BOUND_URL
    assert PASSWORD not in json.dumps(st)


def test_sync_noop_without_credentials_file(tmp_path):
    home = tmp_path / "mhome"
    obs = home / "observatory"
    obs.mkdir(parents=True)
    (obs / "tuwunel.toml").write_text(
        '[global]\naddress = "100.86.76.11"\nport = 18008\n', encoding="utf-8"
    )
    paths = ObservatoryPaths(home)
    assert provision_mod.sync_owner_homeserver_url(paths) == BOUND_URL
    assert not paths.owner_credentials.exists()
