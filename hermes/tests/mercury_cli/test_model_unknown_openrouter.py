"""Regression: openrouter default must not show as unknown.

``hermes.model.default`` stores the provider-RELATIVE id (``meta/...``,
``openai/...``) with the provider in ``model.provider`` (``openrouter``).
Two failure modes made CLI and Matrix `/model` show ``unknown`` after a
restart even though the user had just selected a default:

- direct-file readers (``cli.load_cli_config``, ``gateway._load_gateway_config``,
  gateway ``/model`` persist) bypassed the unified-file ``hermes:`` subtree
  contract, resolving an empty default while ``load_config()`` saw the real one;
- slash-bearing bare ids were mistaken for qualified selectors when deriving
  providers or normalizing wire ids (``openrouter/openrouter/...`` /
  ``No API key found for "meta"``).
"""

from __future__ import annotations

import mercury_cli.omp_sync as omp_sync
from mercury_cli.config import unwrap_hermes_subtree
from mercury_cli.model_normalize import normalize_model_for_provider
from mercury_cli.omp_sync import derive_slot_provider, qualify_omp_model


def test_normalize_strips_openrouter_self_prefix():
    assert (
        normalize_model_for_provider(
            "openrouter/meta/muse-spark-1.3-contributor", "openrouter"
        )
        == "meta/muse-spark-1.3-contributor"
    )


def test_normalize_keeps_bare_vendor_slash_id():
    assert (
        normalize_model_for_provider("meta/muse-spark-1.3-contributor", "openrouter")
        == "meta/muse-spark-1.3-contributor"
    )
    assert (
        normalize_model_for_provider("openai/gpt-5.4", "openrouter")
        == "openai/gpt-5.4"
    )


def test_derive_slot_provider_prefers_hermes_view():
    # Bare relative slot ("meta/...") would split to provider "meta";
    # the just-saved hermes view is authoritative.
    assert derive_slot_provider("meta/muse-spark-1.3", "openrouter") == "openrouter"
    assert (
        derive_slot_provider("openrouter/meta/muse-spark-1.3", "") == "openrouter"
    )
    assert derive_slot_provider("", "") == ""


def test_unwrap_hermes_subtree_plain_passthrough():
    plain = {"model": {"default": "x", "provider": "openrouter"}}
    assert unwrap_hermes_subtree(plain) is plain


def test_unwrap_hermes_subtree_unified():
    unified = {
        "hermes": {"model": {"default": "meta/x", "provider": "openrouter"}},
        "models": {"default": "openrouter/meta/x"},
    }
    assert unwrap_hermes_subtree(unified) == {
        "model": {"default": "meta/x", "provider": "openrouter"}
    }


def test_unwrap_hermes_subtree_no_model_passthrough():
    assert unwrap_hermes_subtree({"omp": {}}) == {"omp": {}}
    assert unwrap_hermes_subtree({}) == {}
    assert unwrap_hermes_subtree(None) is None


def _write(path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_sync_repairs_bare_fallback_slot(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        "hermes:\n"
        "  model:\n"
        "    default: meta/muse-spark-1.3-contributor\n"
        "    provider: openrouter\n"
        "models:\n"
        "  default: openrouter/meta/muse-spark-1.3-contributor\n"
        "  fallback: meta/other-model\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync,
        "_read_model_default",
        lambda: ("openrouter", "meta/muse-spark-1.3-contributor"),
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    text = cfg.read_text(encoding="utf-8")
    assert "fallback: openrouter/meta/other-model" in text
    # Second run is a no-op: repair is idempotent.
    before = text
    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    assert cfg.read_text(encoding="utf-8") == before


def test_sync_repairs_bare_fallback_chain(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        "hermes:\n"
        "  model:\n"
        "    default: meta/muse-spark-1.3-contributor\n"
        "    provider: openrouter\n"
        "models:\n"
        "  default: openrouter/meta/muse-spark-1.3-contributor\n"
        "  fallback: openrouter/meta/other-model\n"
        "  fallback_chain: [meta/other-model, openai/gpt-5]\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync,
        "_read_model_default",
        lambda: ("openrouter", "meta/muse-spark-1.3-contributor"),
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    import yaml

    models = yaml.safe_load(cfg.read_text(encoding="utf-8"))["models"]
    assert models["fallback_chain"] == [
        "openrouter/meta/other-model",
        "openrouter/openai/gpt-5",
    ]


def test_sync_never_invents_fallback(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write(
        cfg,
        "hermes:\n"
        "  model:\n"
        "    default: meta/muse-spark-1.3-contributor\n"
        "    provider: openrouter\n"
        "models:\n"
        "  default: openrouter/meta/muse-spark-1.3-contributor\n"
        "  fallback: ''\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync,
        "_read_model_default",
        lambda: ("openrouter", "meta/muse-spark-1.3-contributor"),
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    import yaml

    models = yaml.safe_load(cfg.read_text(encoding="utf-8"))["models"]
    assert not models.get("fallback")


def test_gateway_falls_back_to_canonical_unified_default(tmp_path, monkeypatch):
    import yaml

    import gateway.run as gateway_run

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"onboarding": {"seen": {}}}), encoding="utf-8"
    )
    canonical = tmp_path / "mercury" / "config.yaml"
    canonical.parent.mkdir()
    canonical.write_text(
        yaml.safe_dump(
            {
                "hermes": {
                    "model": {
                        "default": "meta/muse-spark-1.3-contributor",
                        "provider": "openrouter",
                    }
                },
                "models": {"default": "openrouter/meta/muse-spark-1.3-contributor"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setenv("MERCURY_CONFIG", str(canonical))

    cfg = gateway_run._load_gateway_config()
    assert cfg.get("model", {}).get("default") == "meta/muse-spark-1.3-contributor"
    assert gateway_run._resolve_gateway_model(cfg) == "meta/muse-spark-1.3-contributor"


def test_gateway_prefers_explicit_unified_file(tmp_path, monkeypatch):
    import yaml

    import gateway.run as gateway_run

    canonical = tmp_path / "config.yaml"
    canonical.write_text(
        yaml.safe_dump(
            {
                "hermes": {
                    "model": {
                        "default": "meta/muse-spark-1.3-contributor",
                        "provider": "openrouter",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(canonical))

    cfg = gateway_run._load_gateway_config(config_path=canonical)
    assert cfg.get("model", {}).get("default") == "meta/muse-spark-1.3-contributor"


def test_qualify_bare_ids_for_all_slots():
    assert (
        qualify_omp_model("meta/other-model", "openrouter")
        == "openrouter/meta/other-model"
    )
    full = "openrouter/meta/other-model"
    assert qualify_omp_model(full, "openrouter") == full


def test_update_config_preserves_unified_siblings_and_slash_default(tmp_path, monkeypatch):
    import yaml
    from mercury_cli.auth import _update_config_for_provider
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "hermes": {
                    "model": {"default": "meta/muse-spark-1.3", "provider": "openrouter"}
                },
                "models": {"default": "openrouter/meta/muse-spark-1.3"},
                "omp": {"marker": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    _update_config_for_provider(
        "nous", "https://inference.example.com/v1/", default_model="anthropic/x"
    )
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert data["hermes"]["model"]["default"] == "meta/muse-spark-1.3"
    assert data["hermes"]["model"]["provider"] == "nous"
    assert data["models"] == {"default": "openrouter/meta/muse-spark-1.3"}
    assert data["omp"] == {"marker": True}
