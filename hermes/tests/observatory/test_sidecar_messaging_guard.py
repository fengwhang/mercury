"""Source guard: no Phase-3-era 'sidecar is future work' wording in user outputs.

BUG 2 (0.0.20 VM): Mercury told the user the sidecar "hasn't landed yet"
while the tarball already shipped the full observatory package (provision.py
+ sidecar_main.py). The code SHIPS — so any user-visible string claiming the
sidecar is unshipped/future work is a stale-string bug. It must describe the
CURRENT state instead: the sidecar exists; when absent it needs starting
(`mercury setup observatory` / `--install-sidecar`), not waiting.

This guard covers every user-facing output path (rendered files users read,
the owner-credentials note users `cat`, the setup card, the E2EE remedy, the
user guide). Code comments/docstrings are NOT covered here — they are fine
unless they confuse; only user strings are the bug.

Each test fails on a plausible regression: someone re-adding 'Phase 3',
'lands in Phase 3', "hasn't landed", 'not shipped', 'coming soon',
'still landing', 'until the sidecar ships', "rooms won't fill", or
future-work wording to an output users see.
"""
from __future__ import annotations

import re
from pathlib import Path

from observatory import config_gen
from observatory import provision as provision_mod

# Stale-shipment phrasing: any of these in a user-facing string means the
# sidecar is being described as future work instead of shippable/runnable.
_BANNED = [
    r"lands?\s+in\s+phase",      # 'Sidecar daemon lands in Phase 3'
    r"hasn'?t\s+landed",         # "hasn't landed yet"
    r"has\s+not\s+landed",
    r"\bnot\s+landed\b",
    r"\bnot\s+shipped\b",
    r"coming\s+soon",
    r"phase\s*3",                # any Phase-3 roadmap label in outputs
    r"until\s+the\s+sidecar\s+ships",
    r"still\s+landing",
    r"won'?t\s+fill",            # "rooms won't fill up yet"
    r"\bfuture\b",               # rendered outputs/cards never promise future work
]
_BANNED_RX = [re.compile(p, re.IGNORECASE) for p in _BANNED]

# Website guide may legitimately use 'future' for unrelated features, so its
# guard bans sidecar-specific future claims instead of the bare word.
_DOC_BANNED = [p for p in _BANNED if p != r"\bfuture\b"] + [
    r"future\s+(sidecar|systemd|hook|milestone|work\s+.*sidecar)",
    r"sidecar.*future|future.*sidecar",
]
_DOC_BANNED_RX = [re.compile(p, re.IGNORECASE) for p in _DOC_BANNED]


def _assert_ships(text: str, where: str, rx_list: list = None) -> None:
    rx_list = rx_list if rx_list is not None else _BANNED_RX
    for rx in rx_list:
        assert not rx.search(text), f"{where} reads as future work ({rx.pattern!r}): {text[:200]!r}"


_TOML_KWARGS = dict(
    database_path="/home/x/.mercury/observatory/tuwunel-db",
    appservice_dir="/home/x/.mercury/observatory/appservices",
    registration_token="reg-token-abc123",
)


class TestRenderedOutputsShip:
    def test_tuwunel_toml_comment_is_present_tense(self):
        _assert_ships(
            config_gen.render_tuwunel_toml(**_TOML_KWARGS),
            "render_tuwunel_toml",
        )

    def test_appservice_registration_yaml_is_present_tense(self):
        _assert_ships(
            config_gen.render_appservice_registration_yaml(
                url="http://127.0.0.1:18090", as_token="a" * 16, hs_token="b" * 16
            ),
            "render_appservice_registration_yaml",
        )

    def test_homeserver_unit_is_present_tense(self):
        _assert_ships(
            config_gen.render_homeserver_unit(
                exec_path="/home/x/.mercury/observatory/bin/tuwunel",
                config_path="/home/x/.mercury/observatory/tuwunel.toml",
                log_dir="/home/x/.mercury/observatory/logs",
            ),
            "render_homeserver_unit",
        )

    def test_sidecar_unit_is_present_tense(self):
        from observatory.sidecar_main import render_sidecar_unit

        _assert_ships(
            render_sidecar_unit(
                python_bin="/home/x/.venv/bin/python",
                hermes_root="/home/x/mercury/hermes",
                mercury_home="/home/x/.mercury",
                log_dir="/home/x/.mercury/observatory/logs",
            ),
            "render_sidecar_unit",
        )


class TestOwnerNoteShips:
    def test_owner_credentials_note_names_setup_not_phase3(self):
        # The note ships inside owner-credentials.json, which the user guide
        # tells users to `cat` — it is user-visible. Check the template
        # literal provision writes (no live homeserver needed).
        src = Path(provision_mod.__file__).read_text(encoding="utf-8")
        m = re.search(r'"note":\s*"([^"]*)"\s*"([^"]*)"', src)
        assert m is not None, "owner-credentials note template missing in provision.py"
        _assert_ships(m.group(1) + " " + m.group(2), "owner-credentials note")
        assert "mercury setup observatory" in (m.group(1) + m.group(2)), (
            "note must say how to start/repair, not just what the account is"
        )


class TestRemedyAndCardShip:
    def test_e2ee_remedy_is_present_tense(self):
        from observatory.e2ee import E2EE_REMEDY

        _assert_ships(E2EE_REMEDY, "E2EE_REMEDY")

    def test_setup_card_is_present_tense(self, tmp_path, capsys, monkeypatch):
        import json

        import mercury_cli.setup as setup_mod

        creds = tmp_path / "owner-credentials.json"
        creds.write_text(
            json.dumps({"user_id": "@merc-owner:mercury.local"}), encoding="utf-8"
        )
        status = {
            "provisioned": True,
            "config_exists": True,
            "binary_installed": True,
            "owner_credentials_exist": True,
            "owner_credentials_path": str(creds),
            "homeserver_url": "http://127.0.0.1:18008",
            "homeserver_reachable": True,
            "unit_active": True,
            "unit_name": "mercury-observatory-homeserver.service",
            "enabled": True,
            "e2ee": True,
            "observatory_dir": str(tmp_path),
        }
        monkeypatch.setattr(setup_mod, "_tailscale_status", lambda _obs=None: {})
        setup_mod._print_observatory_setup_card(status, {})
        out = capsys.readouterr().out
        assert "first login" in out.lower(), "card must still render the login card"
        _assert_ships(out, "setup card")


class TestUserGuideShips:
    def test_matrix_observatory_guide_is_present_tense(self):
        guide = (
            Path(__file__).resolve().parents[3]
            / "website"
            / "docs"
            / "user-guide"
            / "messaging"
            / "matrix-observatory.md"
        )
        assert guide.is_file(), f"user guide missing at {guide}"
        text = guide.read_text(encoding="utf-8")
        _assert_ships(text, "matrix-observatory.md", _DOC_BANNED_RX)
        assert "mercury setup observatory" in text, (
            "guide must say how to start/repair the sidecar"
        )
