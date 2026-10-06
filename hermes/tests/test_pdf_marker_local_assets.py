"""Bundled Marker OCR must reject missing weights before model construction."""

import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


HELPER = Path(__file__).resolve().parents[1] / "skills/productivity/pdf/scripts/extract_marker.py"


def _helper():
    spec = importlib.util.spec_from_file_location("mercury_marker_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _marker(monkeypatch, settings):
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", *vars(settings)):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    constructed = []
    for name in ("marker", "marker.converters", "surya"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "surya.settings", SimpleNamespace(settings=settings))
    monkeypatch.setitem(sys.modules, "marker.config.parser", SimpleNamespace(
        ConfigParser=lambda config: SimpleNamespace(generate_config_dict=lambda: config)))
    monkeypatch.setitem(sys.modules, "marker.models", SimpleNamespace(
        create_model_dict=lambda: constructed.append(True) or {}))
    monkeypatch.setitem(sys.modules, "marker.converters.pdf", SimpleNamespace(
        PdfConverter=lambda **kwargs: lambda path: SimpleNamespace(markdown="locally extracted")))
    return constructed


def test_missing_marker_weights_never_construct_models(monkeypatch):
    constructed = _marker(monkeypatch, SimpleNamespace(DETECTOR_MODEL_CHECKPOINT="s3://missing/model"))
    with pytest.raises(RuntimeError, match="pre-provisioned local"):
        _helper().convert("scan.pdf")
    assert constructed == []


def test_incomplete_checkpoint_never_constructs_models(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    constructed = _marker(monkeypatch, SimpleNamespace(DETECTOR_MODEL_CHECKPOINT=str(tmp_path)))
    with pytest.raises(RuntimeError, match="No local weights"):
        _helper().convert("scan.pdf")
    assert constructed == []


def test_complete_local_checkpoint_converts_without_enabling_downloads(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.safetensors").write_bytes(b"local-fixture")
    constructed = _marker(monkeypatch, SimpleNamespace(DETECTOR_MODEL_CHECKPOINT=str(tmp_path)))
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")

    _helper().convert("scan.pdf")

    assert constructed == [True]
    assert capsys.readouterr().out == "locally extracted\n"
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_unknown_model_selector_contract_fails_before_construction(monkeypatch):
    constructed = _marker(monkeypatch, SimpleNamespace())
    with pytest.raises(RuntimeError, match="Cannot verify"):
        _helper().convert("scan.pdf")
    assert constructed == []


def test_gguf_backend_missing_local_projector_never_constructs_models(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "model.gguf").write_bytes(b"local-fixture")
    constructed = _marker(monkeypatch, SimpleNamespace(
        SURYA_MODEL_CHECKPOINT=str(tmp_path),
        SURYA_GGUF_LOCAL_MODEL_PATH=str(tmp_path / "model.gguf"),
        SURYA_GGUF_LOCAL_MMPROJ_PATH=None,
    ))
    with pytest.raises(RuntimeError, match="SURYA_GGUF_LOCAL_MMPROJ_PATH"):
        _helper().convert("scan.pdf")
    assert constructed == []


def test_admitted_model_paths_are_resolved_before_loader_consumes_them(tmp_path, monkeypatch):
    home = tmp_path / "home"
    model = home / "models"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}")
    (model / "model.safetensors").write_bytes(b"local-fixture")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DETECTOR_MODEL_CHECKPOINT", "~/models")
    settings = SimpleNamespace(DETECTOR_MODEL_CHECKPOINT="~/models")
    _marker(monkeypatch, settings)

    _helper().convert("scan.pdf")

    assert settings.DETECTOR_MODEL_CHECKPOINT == str(model.resolve())
    assert os.environ["DETECTOR_MODEL_CHECKPOINT"] == str(model.resolve())
