"""Hugging Face provider operations require an explicit self-hosted endpoint."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mercury_cli import auth, runtime_provider
from providers import get_provider_profile


REFUSED_BASES = (
    "", "https://router.huggingface.co/v1", "https://huggingface.co/v1",
    "https://API.HUGGINGFACE.CO.:443/v1", "https://huggingface.co:443/v1",
    "huggingface.co/v1", "ftp://example.invalid/v1", "https:///v1",
)


@pytest.fixture(autouse=True)
def offline_hf(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf-offline-test")
    monkeypatch.delenv("HF_BASE_URL", raising=False)
    monkeypatch.setattr(auth, "read_raw_config", lambda: {})
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: {})
    monkeypatch.setattr(runtime_provider, "load_pool", lambda *args: None)
    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})


@pytest.mark.parametrize("base", REFUSED_BASES)
def test_hf_runtime_refuses_missing_or_prohibited_base_before_transport(monkeypatch, base):
    transport = Mock(side_effect=AssertionError("provider network attempted"))
    monkeypatch.setattr("httpx.Client.send", transport)
    monkeypatch.setenv("HF_BASE_URL", base)
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        runtime_provider.resolve_runtime_provider(requested="huggingface")
    transport.assert_not_called()


@pytest.mark.parametrize("source", ["env", "explicit", "config"])
def test_hf_runtime_preserves_selfhost_authority(monkeypatch, source):
    base = "https://example.invalid/hf/v1"
    args = {"requested": "hf"}
    if source == "env":
        monkeypatch.setenv("HF_BASE_URL", base)
    elif source == "explicit":
        args.update(explicit_base_url=base, explicit_api_key="explicit-token")
    else:
        cfg = {"provider": "huggingface", "base_url": base}
        monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: cfg)
        monkeypatch.setattr(auth, "read_raw_config", lambda: {"model": cfg})
    result = runtime_provider.resolve_runtime_provider(**args)
    assert result["provider"] == "huggingface"
    assert result["base_url"] == base
    assert result["api_key"] == ("explicit-token" if source == "explicit" else "hf-offline-test")


@pytest.mark.parametrize("base", REFUSED_BASES)
def test_hf_explicit_key_cannot_restore_remote_default(monkeypatch, base):
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        runtime_provider.resolve_runtime_provider(
            requested="hf", explicit_api_key="explicit-token", explicit_base_url=base,
        )


def test_hf_pooled_legacy_remote_endpoint_is_refused():
    entry = SimpleNamespace(runtime_base_url="https://router.huggingface.co/v1", runtime_api_key="old-token")
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        runtime_provider._resolve_runtime_from_pool_entry(
            provider="huggingface", entry=entry, requested_provider="hf", model_cfg={"provider": "huggingface"},
        )


@pytest.mark.parametrize("base", REFUSED_BASES)
def test_hf_profile_catalog_refuses_before_request(monkeypatch, base):
    transport = Mock()
    monkeypatch.setattr("mercury_cli.urllib_security.open_credentialed_url", transport)
    profile = get_provider_profile("hf")
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        profile.fetch_models(api_key="hf-offline-test", base_url=base)
    transport.assert_not_called()


def test_hf_selfhost_catalog_is_fetched_by_picker(monkeypatch):
    from io import BytesIO
    from mercury_cli import models

    calls = []
    def transport(req, **kwargs):
        calls.append(req.full_url)
        assert req.get_header("Authorization") == "Bearer hf-offline-test"
        return BytesIO(b'{"data":[{"id":"selfhost-model"}]}')

    monkeypatch.setenv("HF_BASE_URL", "https://example.invalid/hf/v1")
    monkeypatch.setattr("mercury_cli.urllib_security.open_credentialed_url", transport)
    result = models.provider_model_ids("huggingface", force_refresh=True)
    assert "selfhost-model" in result
    assert calls == ["https://example.invalid/hf/v1/models"]


@pytest.mark.parametrize("base", REFUSED_BASES)
def test_hf_picker_retains_static_catalog_without_network(monkeypatch, base):
    from mercury_cli import models

    transport = Mock()
    monkeypatch.setattr("mercury_cli.urllib_security.open_credentialed_url", transport)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda **kwargs: {})
    monkeypatch.setenv("HF_BASE_URL", base)
    assert models.provider_model_ids("hf", force_refresh=True)
    transport.assert_not_called()


def test_hf_explicit_selfhost_resolves_environment_key(monkeypatch):
    result = runtime_provider.resolve_runtime_provider(
        requested="hf", explicit_base_url="https://example.invalid/hf/v1",
    )
    assert result["base_url"] == "https://example.invalid/hf/v1"
    assert result["api_key"] == "hf-offline-test"


@pytest.mark.parametrize("base", ["", "https://router.huggingface.co/v1", "https://example.invalid/hf/v1"])
def test_doctor_hf_probe_requires_selfhost(monkeypatch, tmp_path, capsys, base):
    import sys
    from argparse import Namespace
    from mercury_cli import doctor

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: {}\n")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(doctor, "HERMES_HOME", home)
    monkeypatch.setattr(doctor, "_DHH", str(home))
    monkeypatch.setattr(doctor, "PROJECT_ROOT", project)
    monkeypatch.setattr(doctor, "_safe_which", lambda *args: None)
    monkeypatch.setitem(sys.modules, "model_tools", SimpleNamespace(
        check_tool_availability=lambda *args, **kwargs: ([], []), TOOLSET_REQUIREMENTS={},
    ))
    monkeypatch.setenv("HF_BASE_URL", base)
    monkeypatch.setattr(doctor, "_APIKEY_PROVIDERS_CACHE", None)
    calls = []
    def transport(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr("httpx.get", transport)
    monkeypatch.setattr(auth, "get_nous_auth_status_local", lambda: {})
    monkeypatch.setattr(auth, "get_codex_auth_status", lambda: {})
    monkeypatch.setattr(auth, "get_xai_oauth_auth_status", lambda: {})
    doctor.run_doctor(Namespace(fix=False))
    out = capsys.readouterr().out
    if base == "https://example.invalid/hf/v1":
        assert calls == [base + "/models"]
        assert "Hugging Face" in out
    else:
        assert calls == []
        assert "self-hosted" in out and "HF_BASE_URL" in out


def test_hf_registry_does_not_resurrect_catalog_endpoint(monkeypatch):
    from mercury_cli import providers
    monkeypatch.setattr("agent.models_dev.get_provider_info", lambda *args, **kwargs: SimpleNamespace(
        id="huggingface", name="HuggingFace", env=("HF_TOKEN",),
        api="https://router.huggingface.co/v1", doc="", models={},
    ))
    assert providers.get_provider("huggingface", allow_network=False).base_url == ""


def test_hf_plugin_reregistration_cannot_restore_hosted_models_url(monkeypatch):
    from dataclasses import replace
    import providers

    profile = get_provider_profile("hf")
    monkeypatch.setattr(providers, "_REGISTRY", dict(providers._REGISTRY))
    monkeypatch.setattr(providers, "_PROVIDER_LIST_CACHE", None)
    providers.register_provider(replace(
        profile, base_url="https://example.invalid/v1",
        models_url="https://router.huggingface.co/v1/models",
    ))
    transport = Mock()
    monkeypatch.setattr("mercury_cli.urllib_security.open_credentialed_url", transport)
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        get_provider_profile("hf").fetch_models(
            api_key="hf-offline-test", base_url="https://example.invalid/v1",
        )
    transport.assert_not_called()


def test_hf_model_config_endpoint_keeps_authority_over_env(monkeypatch):
    cfg = {"provider": "huggingface", "base_url": "https://example.invalid/config/v1"}
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: cfg)
    monkeypatch.setattr(auth, "read_raw_config", lambda: {"model": cfg})
    monkeypatch.setenv("HF_BASE_URL", "https://router.huggingface.co/v1")
    result = runtime_provider.resolve_runtime_provider(requested="huggingface")
    assert result["base_url"] == cfg["base_url"]


def test_hf_named_alias_override_cannot_restore_hosted_endpoint(monkeypatch):
    custom = {"providers": {"hf": {"base_url": "https://router.huggingface.co/v1", "api_key": "offline-key"}}}
    monkeypatch.setattr(runtime_provider, "load_config", lambda: custom)
    with pytest.raises(auth.AuthError, match="self-hosted.*HF_BASE_URL"):
        runtime_provider.resolve_runtime_provider(requested="hf")
