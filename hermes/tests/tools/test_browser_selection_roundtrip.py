"""Write-then-resolve round trip for browser provider selections.

Regression coverage: under the unified Mercury config (MERCURY_CONFIG),
setup persists ``browser.cloud_provider`` nested under ``hermes:``.
``read_raw_config()`` unwraps that subtree, but ``read_raw_config_readonly()``
shared the same ``_RAW_CONFIG_CACHE`` WITHOUT unwrapping — so a readonly-first
call (the shared-metrics gate runs 2-3x per agent turn) poisoned the cache
with the whole-file shape and ``_resolve_cloud_provider_uncached()`` saw no
``browser`` section: a saved ``nous`` selection silently resolved to local.

These tests pin the contract: whatever the setup picker writes (nous, vendor,
local) is what the runtime resolves, regardless of raw-reader call order.
Real ``save_config`` + real readers; no config mocks.
"""

import os

import pytest


def _reset_state():
    import mercury_cli.config as config_mod
    import tools.browser_tool as browser_tool

    config_mod._RAW_CONFIG_CACHE.clear()
    browser_tool._cached_cloud_provider = None
    browser_tool._cloud_provider_resolved = False
    browser_tool._cached_cloud_provider_scope = None
    try:
        browser_tool._cached_cloud_providers.clear()
    except AttributeError:
        pass


@pytest.fixture()
def unified_home(tmp_path, monkeypatch):
    """Unified-layout home: setup nests hermes settings under hermes:."""
    home = tmp_path / "mercury"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("MERCURY_HOME", str(home))
    monkeypatch.setenv("MERCURY_CONFIG", str(home / "config.yaml"))
    _reset_state()
    yield home
    _reset_state()


@pytest.fixture()
def plain_home(tmp_path, monkeypatch):
    """Plain hermes layout: top-level sections, no MERCURY_CONFIG."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("MERCURY_CONFIG", raising=False)
    monkeypatch.delenv("MERCURY_HOME", raising=False)
    _reset_state()
    yield home
    _reset_state()


def _write_browser_selection(cloud_value=None, *, managed=False):
    """Persist a browser pick through the real setup write path."""
    from mercury_cli.config import save_config
    from mercury_cli.tools_config import _write_provider_config

    row = {"browser_provider": cloud_value} if cloud_value else {}
    cfg = {}
    _write_provider_config(
        row, cfg, managed_feature="browser" if managed else None
    )
    save_config(cfg)
    return cfg


def _resolve_name():
    """Resolve through the real runtime path; return provider name or None."""
    import tools.browser_tool as browser_tool

    browser_tool._cached_cloud_provider = None
    browser_tool._cloud_provider_resolved = False
    browser_tool._cached_cloud_provider_scope = None
    try:
        browser_tool._cached_cloud_providers.clear()
    except AttributeError:
        pass
    resolved = browser_tool._resolve_cloud_provider_uncached()
    return resolved.name if resolved is not None else None


def _readonly_first():
    """Touch the readonly reader first — the production poison order."""
    from mercury_cli.config import read_raw_config_readonly

    read_raw_config_readonly()


class TestUnifiedRoundTrip:
    @pytest.mark.parametrize("poison", [False, True])
    def test_nous_selection_resolves_managed_provider(self, unified_home, poison):
        _write_browser_selection("browser-use", managed=True)
        if poison:
            _readonly_first()
        assert _resolve_name() == "browser-use"

    @pytest.mark.parametrize("poison", [False, True])
    def test_vendor_selection_resolves_vendor(self, unified_home, poison):
        _write_browser_selection("browserbase")
        if poison:
            _readonly_first()
        assert _resolve_name() == "browserbase"

    @pytest.mark.parametrize("poison", [False, True])
    def test_local_selection_resolves_local(self, unified_home, poison):
        _write_browser_selection("local")
        if poison:
            _readonly_first()
        assert _resolve_name() is None

    def test_raw_readers_agree_both_orders(self, unified_home):
        from mercury_cli.config import read_raw_config, read_raw_config_readonly
        import mercury_cli.config as config_mod

        _write_browser_selection("browser-use", managed=True)
        config_mod._RAW_CONFIG_CACHE.clear()
        assert sorted(read_raw_config().keys()) == sorted(
            read_raw_config_readonly().keys()
        )

        config_mod._RAW_CONFIG_CACHE.clear()
        assert sorted(read_raw_config_readonly().keys()) == sorted(
            read_raw_config().keys()
        )
        # Content pin: readonly-first must not hide the nested selection
        # from the subtree reader browser_tool uses.
        config_mod._RAW_CONFIG_CACHE.clear()
        read_raw_config_readonly()
        assert read_raw_config().get("browser") == {"cloud_provider": "nous"}

    def test_selection_visible_to_shared_helper(self, unified_home):
        from tools.tool_backend_helpers import read_selection, selection_exists

        _write_browser_selection("browser-use", managed=True)
        _readonly_first()
        assert read_selection("browser") == "nous"
        assert selection_exists("browser") is True


class TestPlainRoundTrip:
    @pytest.mark.parametrize("poison", [False, True])
    def test_nous_selection_resolves_managed_provider(self, plain_home, poison):
        _write_browser_selection("browser-use", managed=True)
        if poison:
            _readonly_first()
        assert _resolve_name() == "browser-use"

    def test_local_selection_resolves_local(self, plain_home):
        _write_browser_selection("local")
        _readonly_first()
        assert _resolve_name() is None
