"""Automatic picker/runtime metadata reads use local data, not public catalogs."""

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent import models_dev as md
from mercury_cli import model_catalog as mc
from mercury_cli import model_switch as ms
from mercury_cli.inventory import ConfigContext


REGISTRY = {
    "deepseek": {
        "name": "Local DeepSeek metadata",
        "env": ["DEEPSEEK_API_KEY"],
        "api": "https://configured.example.test/v1",
        "models": {
            "local-vision": {
                "name": "Cached vision model", "tool_call": True,
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "limit": {"context": 128000, "output": 8192},
            },
            "local-text": {
                "name": "Cached text model", "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 64000, "output": 4096},
            },
        },
    },
}
MANIFEST = {"version": 1, "providers": {"nous": {"models": [{"id": "cached/nous-model"}]}}}


@pytest.fixture
def local_metadata(monkeypatch):
    monkeypatch.setattr(md, "_models_dev_cache", {})
    monkeypatch.setattr(md, "_models_dev_cache_time", 0)
    monkeypatch.setattr(md, "_models_dev_retry_after", 0)
    monkeypatch.setattr(md, "_models_dev_refresh_in_flight", False)
    monkeypatch.setattr(mc, "_catalog_cache", None)
    monkeypatch.setattr(mc, "_catalog_cache_source_mtime", 0.0)
    monkeypatch.setattr(mc, "_catalog_swr_inflight", False)
    ms._picker_prewarm_done.clear()
    yield
    ms._picker_prewarm_done.clear()


def _seed_stale():
    md._save_disk_cache(REGISTRY)
    mc._write_disk_cache(MANIFEST)
    old = time.time() - 86400
    for path in (md._get_cache_path(), mc._cache_path()):
        os.utime(path, (old, old))


@pytest.mark.parametrize("cache_state", ["cold", "stale-disk", "stale-memory"])
def test_startup_worker_and_picker_read_local_metadata(local_metadata, monkeypatch, cache_state):
    from mercury_cli import inventory
    from mercury_cli.models import get_curated_nous_model_ids

    if cache_state != "cold":
        _seed_stale()
    if cache_state == "stale-memory":
        md._models_dev_cache = REGISTRY
        md._models_dev_cache_time = time.time() - 86400
        mc._catalog_cache = MANIFEST
        mc._catalog_cache_source_mtime = mc._cache_path().stat().st_mtime
    ctx = ConfigContext(current_provider="", current_model="", current_base_url="",
                        user_providers={}, custom_providers=[], excluded_providers=[])
    monkeypatch.setattr(inventory, "load_picker_context", lambda: ctx)
    contacts = []

    def denied(*args, **kwargs):
        contacts.append(args[0])
        raise OSError("public metadata must not be contacted")

    with patch.object(md.requests, "get", side_effect=denied), patch.object(mc.urllib.request, "urlopen", side_effect=denied):
        worker = ms.prewarm_picker_cache_async()
        assert worker is not None
        worker.join(timeout=10)
        assert not worker.is_alive()
        rows = ms.list_authenticated_providers()
        assert {row["slug"] for row in rows} <= {"opencode-free"}
        data = md.fetch_models_dev(allow_network=False)
        curated = get_curated_nous_model_ids()
        for thread in threading.enumerate():
            if thread.name in {"models-dev-refresh", "model-catalog-swr"}:
                thread.join(timeout=10)
                assert not thread.is_alive()
    assert contacts == []
    if cache_state == "cold":
        shipped = json.loads(mc._shipped_manifest_path().read_text())
        assert curated == [row["id"] for row in shipped["providers"]["nous"]["models"]]
        assert data == {}  # No registry is shipped: unknown remains unknown.
    else:
        assert data == REGISTRY
        assert curated == ["cached/nous-model"]
        assert md._models_dev_cache_time < time.time() - md._MODELS_DEV_CACHE_TTL


@pytest.mark.parametrize("cache_state", ["cold", "stale"])
def test_runtime_resolution_and_image_caps_are_local(local_metadata, cache_state):
    from agent.image_routing import _lookup_supports_vision, decide_image_input_mode
    from mercury_cli.providers import get_provider
    from mercury_cli.runtime_provider import (
        _fallback_api_mode, is_routable_provider, resolve_runtime_provider,
    )

    if cache_state == "stale":
        _seed_stale()
    with patch.object(md.requests, "get", side_effect=OSError("no metadata HTTP")) as http:
        assert _fallback_api_mode("deepseek", "https://configured.example.test/v1") == "chat_completions"
        runtime = resolve_runtime_provider(
            requested="deepseek", explicit_api_key="test-configured-key",
            explicit_base_url="https://configured.example.test/v1",
            target_model="local-vision",
        )
        assert runtime["base_url"] == "https://configured.example.test/v1"
        assert runtime["api_mode"] == "chat_completions"
        assert is_routable_provider("deepseek")
        assert not is_routable_provider("missing-local-provider")
        info = get_provider("deepseek")
        assert info is not None
        caps = _lookup_supports_vision("deepseek", "local-vision", {})
        assert caps is (True if cache_state == "stale" else None)
        assert decide_image_input_mode("deepseek", "local-vision", {}) == (
            "native" if cache_state == "stale" else "text"
        )
        assert _lookup_supports_vision("deepseek", "unknown-model", {}) is None
        assert md.get_model_info("deepseek", "unknown-model") is None
        if cache_state == "stale":
            assert info.name == "Local DeepSeek metadata"
            assert md.lookup_models_dev_context("deepseek", "local-vision") == 128000
            assert _lookup_supports_vision("deepseek", "local-text", {}) is False
            assert decide_image_input_mode("deepseek", "local-text", {}) == "text"
        http.assert_not_called()


def test_operator_picker_refresh_updates_both_metadata_caches(local_metadata):
    from mercury_cli.models import get_curated_nous_model_ids

    response = MagicMock(status_code=200, headers={})
    response.json.return_value = REGISTRY
    payload = MagicMock()
    payload.__enter__.return_value = payload
    payload.read.return_value = json.dumps(MANIFEST).encode()
    with patch.object(md.requests, "get", return_value=response) as registry_http, patch.object(
        mc.urllib.request, "urlopen", return_value=payload
    ) as curated_http:
        ms.list_authenticated_providers(refresh=True)
        registry_http.assert_called_once()
        curated_http.assert_called_once()
        assert md.fetch_models_dev() == REGISTRY
        assert get_curated_nous_model_ids() == ["cached/nous-model"]


def test_configured_catalog_mirror_failure_does_not_contact_upstream(local_metadata):
    config = {"enabled": True, "url": "https://mirror.example.test/catalog.json",
              "ttl_hours": 1, "providers": {}}
    with patch.object(mc, "_load_catalog_config", return_value=config), patch.object(
        mc.urllib.request, "urlopen", side_effect=OSError("mirror unavailable")
    ) as http:
        mc.get_catalog(force_refresh=True)
    assert [call.args[0].full_url for call in http.call_args_list] == [config["url"]]


def test_explicit_network_admission_still_fetches_metadata(local_metadata):
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = REGISTRY
    with patch.object(md.requests, "get", return_value=response) as http:
        info = md.get_model_info("deepseek", "local-vision", allow_network=True)
        assert info is not None and info.context_window == 128000
        http.assert_called_once()


def test_network_denial_overrides_force_refresh(local_metadata):
    _seed_stale()
    with patch.object(md.requests, "get") as registry_http, patch.object(mc.urllib.request, "urlopen") as curated_http:
        assert md.fetch_models_dev(force_refresh=True, allow_network=False) == REGISTRY
        assert mc.get_catalog(force_refresh=True, allow_network=False) == MANIFEST
        registry_http.assert_not_called()
        curated_http.assert_not_called()
