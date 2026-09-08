"""Contract tests for the Matrix Observatory provisioning generators.

Spec: docs/design/matrix-observatory.md §2 component 1 + D16. These test
the PURE generators (observatory/config_gen.py) and the pure version/asset
logic (observatory/tuwunel.py) — no network, no filesystem beyond tmp_path.
The full fetch pipeline is exercised by the install dry-run.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from observatory import config_gen, provision, tuwunel
from observatory.config_gen import ObservatoryPaths


# --- tuwunel.toml ---------------------------------------------------------------

class TestTuwunelToml:
    TOML_KWARGS = dict(
        database_path="/home/x/.mercury/observatory/tuwunel-db",
        appservice_dir="/home/x/.mercury/observatory/appservices",
        registration_token="reg-token-abc123",
    )

    def render(self) -> dict:
        return tomllib.loads(config_gen.render_tuwunel_toml(**self.TOML_KWARGS))["global"]

    def test_parses_and_carries_every_contract_key(self):
        cfg = self.render()
        assert cfg["allow_federation"] is False
        assert cfg["allow_registration"] is False
        assert cfg["registration_token"] == "reg-token-abc123"
        assert cfg["database_path"] == self.TOML_KWARGS["database_path"]
        assert cfg["appservice_dir"] == self.TOML_KWARGS["appservice_dir"]

    def test_closed_server_defaults(self):
        """D2: localhost bind, no federation, no registration — the FINAL
        form is closed even though the owner bootstrap temporarily flips a
        copy to allow_registration = true."""
        cfg = self.render()
        assert cfg["allow_federation"] is False
        assert cfg["allow_registration"] is False
        assert cfg["address"] == "127.0.0.1"
        assert cfg["port"] == config_gen.HOMESERVER_PORT_DEFAULT
        assert cfg["server_name"] == config_gen.SERVER_NAME_DEFAULT

    def test_bootstrap_form_opens_registration_only(self):
        raw = config_gen.render_tuwunel_toml(**self.TOML_KWARGS, allow_registration=True)
        cfg = tomllib.loads(raw)["global"]
        assert cfg["allow_registration"] is True
        # everything else identical to the closed form
        closed = self.render()
        cfg.pop("allow_registration"), closed.pop("allow_registration")
        assert cfg == closed

    def test_empty_token_is_a_programming_error(self):
        with pytest.raises(ValueError):
            config_gen.render_tuwunel_toml(**{**self.TOML_KWARGS, "registration_token": ""})


# --- appservice registration YAML -------------------------------------------------

class TestAppserviceRegistration:
    def render(self) -> dict:
        raw = config_gen.render_appservice_registration_yaml(
            url="http://127.0.0.1:18090",
            as_token="as-tok",
            hs_token="hs-tok",
        )
        return yaml.safe_load(raw)

    def test_contract_fields(self):
        reg = self.render()
        assert reg["id"] == "merc-observatory"
        assert reg["url"] == "http://127.0.0.1:18090"
        assert reg["as_token"] == "as-tok"
        assert reg["hs_token"] == "hs-tok"
        assert reg["sender_localpart"] == "merc-bot"
        assert reg["rate_limited"] is False

    def test_namespace_is_exclusive_anchored_and_prefixed(self):
        users = self.render()["namespaces"]["users"]
        assert len(users) == 1
        ns = users[0]
        assert ns["exclusive"] is True
        # anchored both ends: ^@merc_.*$ — the reserved virtual-user prefix
        assert ns["regex"] == "^@merc_.*$"
        import re
        assert re.match(ns["regex"], "@merc_auth-refactor:mercury.local")
        assert not re.match(ns["regex"], "@owner:mercury.local")
        assert not re.match(ns["regex"], "@xmerc_evil:mercury.local")


# --- systemd user unit ---------------------------------------------------------

class TestHomeserverUnit:
    def render(self) -> str:
        return config_gen.render_homeserver_unit(
            exec_path="/home/x/.mercury/observatory/bin/tuwunel",
            config_path="/home/x/.mercury/observatory/tuwunel.toml",
            log_dir="/home/x/.mercury/observatory/logs",
        )

    def test_exec_and_restart_contract(self):
        unit = self.render()
        assert "ExecStart=/home/x/.mercury/observatory/bin/tuwunel -c /home/x/.mercury/observatory/tuwunel.toml" in unit
        assert "Restart=on-failure" in unit
        assert "Restart=always" not in unit

    def test_user_unit_install_and_logs(self):
        unit = self.render()
        assert "WantedBy=default.target" in unit          # user unit (gateway pattern)
        assert "StandardOutput=append:/home/x/.mercury/observatory/logs/homeserver.log" in unit
        assert "StandardError=append:/home/x/.mercury/observatory/logs/homeserver.log" in unit


# --- version gate + asset selection ------------------------------------------------

class TestVersionGate:
    @pytest.mark.parametrize("tag", ["v1.8.1", "1.8.1", "v1.9.0", "v2.0.0", "v10.0.1"])
    def test_passes(self, tag):
        assert tuwunel.parse_version(tag) == tuwunel.parse_version(tuwunel.MIN_VERSION) \
            or tuwunel.parse_version(tag) > tuwunel.parse_version(tuwunel.MIN_VERSION)
        tuwunel.check_min_version(tag)

    @pytest.mark.parametrize("tag", ["v1.8.0", "v1.7.9", "v0.9.0", "1.8"])
    def test_below_minimum_fails_clearly(self, tag):
        with pytest.raises(tuwunel.TuwunelError, match="1.8.1"):
            tuwunel.check_min_version(tag)

    def test_garbage_tag_fails(self):
        with pytest.raises(tuwunel.TuwunelError):
            tuwunel.check_min_version("not-a-version")


_ASSET_NAMES = [  # real v1.9.0 release layout (verified 2026-09-07)
    "v1.9.0-release-all-aarch64-v8-linux-gnu-tuwunel.zst",
    "v1.9.0-release-all-x86_64-v1-linux-gnu-tuwunel.zst",
    "v1.9.0-release-all-x86_64-v2-linux-gnu-tuwunel.zst",
    "v1.9.0-release-all-x86_64-v3-linux-gnu-tuwunel.zst",
    "v1.9.0-release-all-x86_64-v1-linux-gnu-tuwunel.deb",   # decoy: wrong ext
    "v1.9.0-release-default-x86_64-v1-linux-gnu-tuwunel.zst",  # decoy: profile
]


def _release(names=_ASSET_NAMES):
    return {
        "tag_name": "v1.9.0",
        "prerelease": False,
        "assets": [{"name": n, "browser_download_url": f"https://x/{n}"} for n in names],
    }


class TestAssetSelection:
    def test_x86_picks_lowest_microarch_v1(self):
        name, url = tuwunel.pick_asset(_release(), "x86_64-v1")
        assert name == "v1.9.0-release-all-x86_64-v1-linux-gnu-tuwunel.zst"
        assert url.endswith(name)

    def test_aarch64_exact(self):
        name, _ = tuwunel.pick_asset(_release(), "aarch64-v8")
        assert name == "v1.9.0-release-all-aarch64-v8-linux-gnu-tuwunel.zst"

    def test_falls_back_to_lowest_family_variant(self):
        # host asked for v1 but release only carries v3: family fallback
        only_v3 = [n for n in _ASSET_NAMES if "v3-linux" in n or "aarch64" in n]
        name, _ = tuwunel.pick_asset(_release(only_v3), "x86_64-v1")
        assert name == "v1.9.0-release-all-x86_64-v3-linux-gnu-tuwunel.zst"

    def test_no_binary_asset_fails_with_inventory(self):
        with pytest.raises(tuwunel.TuwunelError, match="no static Tuwunel binary"):
            tuwunel.pick_asset(_release(["v1.9.0-release-all-x86_64-v1-linux-gnu-tuwunel.deb"]), "x86_64-v1")

    def test_latest_release_enforces_gate(self):
        import json

        def fetch(_url):
            return json.dumps({"tag_name": "v1.0.0", "assets": []}).encode()

        with pytest.raises(tuwunel.TuwunelError, match="1.8.1"):
            tuwunel.latest_stable_release(fetch)

    def test_host_arch_tokens(self):
        assert tuwunel.host_asset_arch("x86_64") == "x86_64-v1"
        assert tuwunel.host_asset_arch("amd64") == "x86_64-v1"
        assert tuwunel.host_asset_arch("aarch64") == "aarch64-v8"
        assert tuwunel.host_asset_arch("arm64") == "aarch64-v8"
        with pytest.raises(tuwunel.TuwunelError, match="unsupported CPU"):
            tuwunel.host_asset_arch("riscv64")


# --- idempotence (write-once laws) --------------------------------------------------

class TestWriteOnceIdempotence:
    def test_ensure_config_keeps_existing_file(self, tmp_path: Path):
        paths = ObservatoryPaths(tmp_path)
        assert provision.ensure_config(paths, "token-one") == "created"
        first = paths.toml.read_text()
        assert provision.ensure_config(paths, "token-two") == "kept"
        assert paths.toml.read_text() == first  # never overwritten

    def test_ensure_appservice_registration_stable(self, tmp_path: Path):
        paths = ObservatoryPaths(tmp_path)
        assert provision.ensure_appservice_registration(paths) == "created"
        first = paths.appservice_registration.read_text()
        assert provision.ensure_appservice_registration(paths) == "kept"
        assert paths.appservice_registration.read_text() == first

    def test_secret_files_are_0600(self, tmp_path: Path):
        paths = ObservatoryPaths(tmp_path)
        provision.ensure_config(paths, "tok")
        provision.ensure_appservice_registration(paths)
        assert oct(paths.toml.stat().st_mode)[-3:] == "600"
        assert oct(paths.appservice_registration.stat().st_mode)[-3:] == "600"

    def test_installed_version_requires_binary_and_file(self, tmp_path: Path):
        paths = ObservatoryPaths(tmp_path)
        assert tuwunel.installed_version(paths) is None
        paths.bin_dir.mkdir(parents=True, exist_ok=True)
        paths.version_file.write_text("1.9.0\n")
        assert tuwunel.installed_version(paths) is None  # binary missing
        paths.binary.write_bytes(b"fake")
        assert tuwunel.installed_version(paths) == "1.9.0"
