"""Regression: short (provider-relative) model ids must reach omp qualified.

``hermes.model.default`` stores the SHORT form (``meta/muse-spark-...``)
with the provider in ``model.provider`` (``openrouter``). OpenRouter
catalog ids contain a slash but are NOT mercury-qualified selectors, so a
bare ``"/" in id`` check passes them through raw; omp then resolves
provider ``meta`` with no key mapping (``No API key found for "meta"``).
``qualify_omp_model`` prefixes only when the ``<provider>/`` prefix is
absent (idempotent — re-syncs never write ``openrouter/openrouter/...``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import mercury_cli.omp_sync as omp_sync
from mercury_cli.omp_sync import qualify_omp_model


def test_qualify_prefixes_short_id_with_slash():
    assert qualify_omp_model("meta/muse-spark-1.3-contributor", "openrouter", provider_relative=True) == (
        "openrouter/meta/muse-spark-1.3-contributor"
    )


def test_qualify_prefixes_bare_id():
    assert qualify_omp_model("muse-spark-1.3-contributor", "openrouter") == (
        "openrouter/muse-spark-1.3-contributor"
    )


def test_qualify_other_provider_slash_id_passes_through():
    # Cross-provider qualified ids (e.g. custom providers picked under a
    # different catalog scope) must never gain the picker's provider prefix.
    assert qualify_omp_model("myprov/mymodel", "zai") == "myprov/mymodel"
    assert qualify_omp_model("zai/zai-m1", "zai") == "zai/zai-m1"


def test_qualify_other_provider_bare_id_prefixed():
    assert qualify_omp_model("zai-m1", "zai") == "zai/zai-m1"


def test_qualify_leaves_qualified_id_alone():
    full = "openrouter/meta/muse-spark-1.3-contributor"
    assert qualify_omp_model(full, "openrouter") == full


def test_qualify_empty_provider_passes_through():
    assert qualify_omp_model("meta/muse-spark-1.3-contributor", "") == (
        "meta/muse-spark-1.3-contributor"
    )


def test_qualify_empty_model_stays_empty():
    assert qualify_omp_model("", "openrouter") == ""
    assert qualify_omp_model("   ", "openrouter") == ""


def test_qualify_strips_whitespace_and_trailing_slash():
    assert qualify_omp_model("  meta/x  ", "  openrouter/ ", provider_relative=True) == "openrouter/meta/x"


def _write_config(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_sync_writes_qualified_default_slot(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write_config(
        cfg,
        "hermes:\n"
        "  model:\n"
        "    default: meta/muse-spark-1.3-contributor\n"
        "    provider: openrouter\n"
        "models:\n"
        "  default: ''\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync, "_read_model_default", lambda: ("openrouter", "meta/muse-spark-1.3-contributor")
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    text = cfg.read_text(encoding="utf-8")
    assert "default: openrouter/meta/muse-spark-1.3-contributor" in text
    assert "openrouter/openrouter" not in text


def test_sync_preserves_existing_cross_provider_chain_selectors(tmp_path, monkeypatch):
    """Block-seq fallback_chain + sync must stay valid YAML (no stale items).

    Regression: save_config normalizes flow chains to 4-space block-seq;
    _write_slots replaced the key line but only skipped 2-space items,
    leaving stale `- item` lines behind -> corrupt YAML. The shared-bank
    ensure runs save_config on every fresh setup, so this path is hot.
    """
    import yaml

    cfg = tmp_path / "config.yaml"
    _write_config(
        cfg,
        "hermes:\n"
        "  memory:\n"
        "    provider: mnemosyne\n"
        "models:\n"
        "  default: openrouter/meta/muse-spark-1.3-contributor\n"
        "  fallback: openrouter/meta/other-model\n"
        "  fallback_chain:\n"
        "    - meta/other-model\n"
        "    - openai/gpt-5\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync, "_read_model_default", lambda: ("openrouter", "meta/muse-spark-1.3-contributor")
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    models = yaml.safe_load(cfg.read_text(encoding="utf-8"))["models"]
    assert models["fallback_chain"] == [
        "meta/other-model",
        "openai/gpt-5",
    ]


def test_sync_is_idempotent_on_qualified_slots(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    _write_config(
        cfg,
        "hermes:\n"
        "  memory:\n"
        "    provider: mnemosyne\n"
        "models:\n"
        "  default: openrouter/meta/muse-spark-1.3-contributor\n"
        "  delegate_thinking_level: xhigh\n"
        "  orchestrator_thinking_level: xhigh\n",
    )
    monkeypatch.setenv("MERCURY_CONFIG", str(cfg))
    monkeypatch.setenv("MERCURY_HOME", str(tmp_path))
    monkeypatch.setattr(
        omp_sync, "_read_model_default", lambda: ("openrouter", "meta/muse-spark-1.3-contributor")
    )
    monkeypatch.setattr(omp_sync, "_read_fallback", lambda: None)
    monkeypatch.setattr(omp_sync, "_render_omp", lambda: True)

    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    before = cfg.read_text(encoding="utf-8")
    assert omp_sync.sync_omp_from_setup(quiet=True) is True
    after = cfg.read_text(encoding="utf-8")
    assert before == after
    assert "openrouter/openrouter" not in after


def test_read_fallback_qualifies_short_id(monkeypatch):
    monkeypatch.setattr(
        "mercury_cli.config.load_config",
        lambda: {"fallback_providers": [{"provider": "openrouter", "model": "meta/other-model"}]},
    )
    assert omp_sync._read_fallback() == "openrouter/meta/other-model"


def test_read_fallback_leaves_qualified_id_alone(monkeypatch):
    monkeypatch.setattr(
        "mercury_cli.config.load_config",
        lambda: {
            "fallback_providers": [
                {"provider": "openrouter", "model": "openrouter/meta/other-model"}
            ]
        },
    )
    assert omp_sync._read_fallback() == "openrouter/meta/other-model"


@pytest.mark.parametrize("layout", ["four_spaces", "indentless_sequence", "json"])
def test_shared_model_write_preserves_native_settings_and_permissions(tmp_path, monkeypatch, layout):
    """Saving slots must not consume native sibling settings or corrupt YAML."""
    import json
    import yaml

    whole = {
        "models": {"default": "openai-codex/gpt-6.1-sol",
                   "orchestrator_thinking_level": "xhigh",
                   "context_windows": {"openai-codex/gpt-6.1-sol": 872000}},
        "hermes": {"fallback_providers": [{"provider": "openrouter", "model": "old"}],
                   "agent": {"max_turns": 23}},
        "omp": {"tools": {"approvalMode": "write"}},
    }
    path = tmp_path / "config.yaml"
    if layout == "json":
        text = json.dumps(whole)
    else:
        text = yaml.safe_dump(whole, indent=4 if layout == "four_spaces" else 2, sort_keys=False)
        text = text.replace("omp:\n", "# Native settings stay intact\nomp:\n")
    path.write_text(text)
    path.chmod(0o640)
    monkeypatch.setenv("MERCURY_CONFIG", str(path))
    omp_sync._write_slots({"fallback": "nous/xiaomi/mimo-v2.6-pro",
                          "orchestrator_thinking_level": None})
    saved = yaml.safe_load(path.read_text())
    assert saved["hermes"] == {"agent": {"max_turns": 23}}
    assert saved["omp"] == whole["omp"]
    assert saved["models"] == {
        "default": "openai-codex/gpt-6.1-sol", "fallback": "nous/xiaomi/mimo-v2.6-pro",
        "context_windows": {"openai-codex/gpt-6.1-sol": 872000},
    }
    assert path.stat().st_mode & 0o777 == 0o640
    if layout != "json":
        assert "# Native settings stay intact" in path.read_text()
