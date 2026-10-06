"""Voice-call STT: local-flag honor + qwen3-asr/parakeet command dispatch."""

from __future__ import annotations

import sys
import wave
from pathlib import Path

from tools.transcription_tools import (
    _is_local_stt_provider,
    transcribe_audio,
)


def _make_silent_wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 800)
    return path


def _emit_command(text: str) -> str:
    payload = "import sys; open(sys.argv[1], 'w').write(" + repr(text) + ")"
    return f'"{sys.executable}" -c "{payload}" {{output_path}}'


def test_local_flag_opts_command_provider_out_of_upload_cap() -> None:
    cfg = {"providers": {"qwen3-asr": {"type": "command", "command": "x", "local": True}}}
    assert _is_local_stt_provider("qwen3-asr", cfg) is True
    assert _is_local_stt_provider("other-cli", {"providers": {}}) is False
    assert _is_local_stt_provider("local", {}) is True
    assert _is_local_stt_provider("openai", {}) is False


def test_qwen3_asr_transcribes_via_command_registry(tmp_path: Path) -> None:
    from tools import transcription_tools as tt

    saved = tt._load_stt_config
    audio = _make_silent_wav(tmp_path / "clip.wav")
    cfg = {
        "provider": "qwen3-asr",
        "qwen3-asr": {"model": "Qwen3-ASR-1.7B", "language": "en"},
        "providers": {"qwen3-asr": {
            "type": "command", "local": True,
            "command": _emit_command("hello qwen"), "model": "Qwen3-ASR-1.7B",
        }},
    }
    tt._load_stt_config = lambda: cfg  # type: ignore[assignment]
    try:
        result = transcribe_audio(str(audio))
    finally:
        tt._load_stt_config = saved  # type: ignore[assignment]
    assert result["success"] is True
    assert result["transcript"] == "hello qwen"
    assert result["provider"] == "qwen3-asr"


def test_parakeet_transcribes_via_command_registry(tmp_path: Path) -> None:
    from tools import transcription_tools as tt

    saved = tt._load_stt_config
    audio = _make_silent_wav(tmp_path / "clip.wav")
    cfg = {
        "provider": "parakeet",
        "providers": {"parakeet": {
            "type": "command", "local": True,
            "command": _emit_command("hello parakeet"),
        }},
    }
    tt._load_stt_config = lambda: cfg  # type: ignore[assignment]
    try:
        result = transcribe_audio(str(audio))
    finally:
        tt._load_stt_config = saved  # type: ignore[assignment]
    assert result["success"] is True
    assert result["transcript"] == "hello parakeet"
    assert result["provider"] == "parakeet"


def test_sidecar_overlays_configured_command_provider(tmp_path, monkeypatch):
    import shlex
    from observatory.voice_call_stt import load_sidecar_stt_config, transcribe_chunk
    from tools import transcription_tools as tt

    # A real command provider reports the arguments it actually received.
    cli = tmp_path / "fake-asr.py"
    cli.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('|'.join(sys.argv[2:]))\n"
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(cli))} {{output_path}} {{model}} {{language}}"
    stored = {
        "provider": "qwen3-asr",
        "qwen3-asr": {"model": "stored-model", "language": "en"},
        "providers": {"qwen3-asr": {
            "type": "command", "local": True, "command": command,
            "model": "stored-model", "language": "en",
        }},
    }
    monkeypatch.setattr(tt, "_load_stt_config", lambda: stored)
    cfg = load_sidecar_stt_config({"model": "overlay-model", "language": "es"})
    audio = _make_silent_wav(tmp_path / "browser.wav").read_bytes()
    result = transcribe_chunk(audio, "audio/wav", cfg)
    assert result["success"], result
    assert result["transcript"] == "overlay-model|es"
    assert stored["providers"]["qwen3-asr"]["model"] == "stored-model"
    assert stored["qwen3-asr"]["language"] == "en"
