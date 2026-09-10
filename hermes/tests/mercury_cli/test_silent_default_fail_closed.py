"""Fail-closed silent-model regressions (VM: glm-5.2 active with zero user intent).

Every path that used to silently land the user on an unchosen model must now
fail closed ("" / None + an explicit `mercury model` remedy) or require an
explicit pick. A silent default to ANY model the user didn't pick is the bug,
not the version number — so these tests pin behavior, not the glm-5.3 string,
except where the shipped-manifest contract is the point.

Real files throughout: the stale-cache repro writes an actual
``model_catalog.json`` into a tmp HERMES_HOME and reads the real shipped
manifest from the checkout.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + reset module-level catalog cache per test."""
    home = tmp_path / ".mercury"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from mercury_cli import model_catalog
    importlib.reload(model_catalog)
    yield home
    model_catalog.reset_cache()


def _write_disk_cache(home: Path, manifest: dict) -> None:
    cache = home / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "model_catalog.json").write_text(json.dumps(manifest))


def _stale_glm52_manifest() -> dict:
    """Disk cache as left by a pre-fix fetch: default still glm-5.2."""
    return {
        "version": 1,
        "updated_at": "2026-08-01T00:00:00Z",  # older than the shipped file
        "metadata": {"source": "test-stale"},
        "providers": {
            "openrouter": {
                "metadata": {},
                "models": [
                    {"id": "z-ai/glm-5.3"},
                    {"id": "z-ai/glm-5.2", "default": True},
                ],
            },
            "nous": {
                "metadata": {},
                "models": [
                    {"id": "z-ai/glm-5.3"},
                    {"id": "z-ai/glm-5.2", "default": True},
                ],
            },
        },
    }


class TestPickSilentDefaultFailClosed:
    def test_returns_catalog_default_when_available(self):
        from mercury_cli.models import (
            get_preferred_silent_default_model,
            pick_silent_default_model,
        )
        preferred = get_preferred_silent_default_model("openrouter")
        assert pick_silent_default_model(
            ["other/model", preferred], provider="openrouter"
        ) == preferred

    def test_never_escalates_to_first_entry(self):
        """The catalog default missing from the list must NOT fall back to
        entry [0] (aggregator flagship / stale glm) — fail closed instead."""
        from mercury_cli.models import pick_silent_default_model
        assert (
            pick_silent_default_model(
                ["anthropic/claude-fable-5", "z-ai/glm-5.2"],
                provider="openrouter",
            )
            == ""
        )

    def test_empty_list_stays_empty(self):
        from mercury_cli.models import pick_silent_default_model
        assert pick_silent_default_model([], provider="nous") == ""


class TestStaleCacheGuard:
    def test_stale_glm52_cache_loses_to_shipped_manifest(self, isolated_home):
        """VM repro: stale disk cache labeling glm-5.2 must never surface —
        the shipped manifest (newer updated_at) wins."""
        from mercury_cli import model_catalog
        _write_disk_cache(isolated_home, _stale_glm52_manifest())
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            for provider in ("openrouter", "nous"):
                resolved = model_catalog.get_default_model_from_cache(provider)
                assert resolved is not None
                assert "glm-5.2" not in resolved, (
                    f"{provider}: stale cache leaked {resolved!r}"
                )
            fetch.assert_not_called()

    def test_stale_cache_never_reaches_preferred_default(self, isolated_home):
        from mercury_cli.models import get_preferred_silent_default_model
        from mercury_cli import model_catalog
        _write_disk_cache(isolated_home, _stale_glm52_manifest())
        with patch.object(model_catalog, "_fetch_manifest"):
            for provider in ("openrouter", "nous"):
                assert "glm-5.2" not in get_preferred_silent_default_model(provider)

    def test_fresh_rotation_still_honored(self, isolated_home):
        """A cache NEWER than the shipped manifest is a genuine remote
        rotation — it must keep winning (rotate-without-release intact)."""
        from mercury_cli import model_catalog
        manifest = _stale_glm52_manifest()
        manifest["updated_at"] = "2099-01-01T00:00:00Z"
        manifest["providers"]["openrouter"]["models"] = [
            {"id": "z-ai/glm-5.3"},
            {"id": "future/glm-9", "default": True},
        ]
        _write_disk_cache(isolated_home, manifest)
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            assert (
                model_catalog.get_default_model_from_cache("openrouter")
                == "future/glm-9"
            )
            fetch.assert_not_called()

    def test_constant_matches_shipped_manifest(self):
        """The offline fallback constant must equal the shipped label, or a
        fresh install with no cache silently diverges from the manifest."""
        from mercury_cli import model_catalog
        from mercury_cli.models import PREFERRED_SILENT_DEFAULT_MODEL
        repo_root = Path(model_catalog.__file__).resolve().parent.parent
        manifest = json.loads(
            (repo_root / "website" / "static" / "api" / "model-catalog.json")
            .read_text(encoding="utf-8")
        )
        for provider in ("openrouter", "nous"):
            labeled = [
                m["id"]
                for m in manifest["providers"][provider]["models"]
                if m.get("default")
            ]
            assert labeled == [PREFERRED_SILENT_DEFAULT_MODEL]


class TestTuiResolveModelFailClosed:
    def test_no_env_no_config_returns_empty_never_glm(self, monkeypatch):
        """_resolve_model with zero user intent must return "" — the stale
        hardcoded glm-5.2 literal and the catalog-default fallback are gone."""
        from tui_gateway import server
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.setattr(server, "_load_cfg", lambda: {})
        assert server._resolve_model() == ""

    def test_catalog_outage_returns_empty_never_glm(self, monkeypatch):
        from tui_gateway import server
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.setattr(server, "_load_cfg", lambda: {})
        with patch(
            "mercury_cli.models.get_preferred_silent_default_model",
            side_effect=RuntimeError("catalog unreachable"),
        ):
            assert server._resolve_model() == ""

    def test_explicit_env_still_wins(self, monkeypatch):
        from tui_gateway import server
        monkeypatch.setenv("HERMES_MODEL", "custom/my-model")
        assert server._resolve_model() == "custom/my-model"

    def test_explicit_config_still_wins(self, monkeypatch):
        from tui_gateway import server
        monkeypatch.delenv("HERMES_MODEL", raising=False)
        monkeypatch.delenv("HERMES_INFERENCE_MODEL", raising=False)
        monkeypatch.setattr(
            server, "_load_cfg", lambda: {"model": {"default": "z-ai/glm-5.2"}}
        )
        # An explicitly configured model (even 5.2) is the user's choice.
        assert server._resolve_model() == "z-ai/glm-5.2"


class TestRecommendedDefaultEndpointFailClosed:
    def test_non_nous_list_without_catalog_default_returns_empty(self):
        """The suggestion endpoint must answer "" (not entry [0]) when the
        provider's curated list doesn't carry the catalog default — the GUI
        then shows the confirm card with no pre-persisted choice."""
        from mercury_cli import web_server
        payload = {
            "providers": [
                {"slug": "acme", "models": ["acme/flagship-1", "acme/cheap-1"]}
            ]
        }
        with patch(
            "mercury_cli.inventory.build_models_payload", return_value=payload
        ), patch(
            "mercury_cli.inventory.load_picker_context", return_value=object()
        ):
            result = web_server.get_recommended_default_model(provider="acme")
        assert result["model"] == ""
