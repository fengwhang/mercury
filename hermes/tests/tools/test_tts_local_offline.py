"""Local speech must refuse missing assets before any downloader or SDK loader."""
import json
import subprocess
from unittest.mock import Mock

import pytest

from tools import tts_tool as tts
from tools import tts_tool_local as local
from tools.tts_tool_lifecycle import warm_tts_provider


@pytest.mark.parametrize("entrypoint", ["tool", "loader", "warm"])
def test_missing_piper_assets_never_download(entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    process = Mock(side_effect=AssertionError("unexpected download process"))
    sdk = Mock(side_effect=AssertionError("unexpected SDK initializer"))
    monkeypatch.setattr(subprocess, "run", process)
    monkeypatch.setattr(tts, "_import_piper", lambda: sdk)
    config = {"provider": "piper", "piper": {"voices_dir": str(tmp_path)}}
    monkeypatch.setattr(tts, "_load_tts_config", lambda: config)
    if entrypoint == "tool":
        result = json.loads(tts.text_to_speech_tool("hello", str(tmp_path / "out.wav")))
        assert result["success"] is False
        error = result["error"]
    elif entrypoint == "warm":
        result = warm_tts_provider(config)
        assert result["warmed"] is False
        assert result["action"] == "error"
        error = result["error"]
    else:
        with pytest.raises(RuntimeError) as exc:
            local._load_piper_voice_for_config(config)
        error = str(exc.value)
    assert "local" in error.lower()
    assert "tts.piper.voice" in error
    assert ".onnx.json" in error
    process.assert_not_called()
    sdk.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["tool", "loader", "warm"])
def test_missing_kitten_assets_never_initialize_downloader(entrypoint, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "huggingface"))
    sdk = Mock(side_effect=AssertionError("unexpected SDK download"))
    monkeypatch.setattr(tts, "_import_kittentts", lambda: sdk)
    config = {"provider": "kittentts"}
    monkeypatch.setattr(tts, "_load_tts_config", lambda: config)
    if entrypoint == "tool":
        result = json.loads(tts.text_to_speech_tool("hello", str(tmp_path / "out.wav")))
        assert result["success"] is False
        error = result["error"]
    elif entrypoint == "warm":
        result = warm_tts_provider(config)
        assert result["warmed"] is False
        assert result["action"] == "error"
        error = result["error"]
    else:
        with pytest.raises(RuntimeError) as exc:
            local._load_kittentts_model_for_config(config)
        error = str(exc.value)
    assert "local" in error.lower()
    assert "tts.kittentts.model" in error
    assert "config.json" in error
    sdk.assert_not_called()
