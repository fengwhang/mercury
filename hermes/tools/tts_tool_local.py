"""Local on-device TTS engines for ``tools.tts_tool``: NeuTTS, Piper, KittenTTS.

All three synthesize WAV natively; :func:`_finalize_wav_output` converts/renames to the requested
container. Piper and KittenTTS keep loaded models in small LRU caches registered in
``_LOCAL_TTS_MODEL_CACHES`` so warm/release can pre-load or drop them. ``_import_piper`` /
``_import_kittentts`` are resolved through the origin module at call time (test monkeypatches).
"""

from __future__ import annotations

import json
import os
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

from tools.tts_tool_delivery import _finalize_wav_output, _origin, _section, _wav_sidecar_path
from tools.neutts_synth import _cached_hf_file, _resolve_neutts_assets

logger = logging.getLogger("tools.tts_tool")

DEFAULT_KITTENTTS_MODEL = "KittenML/kitten-tts-nano-0.8-int8"  # 25MB
DEFAULT_KITTENTTS_VOICE = "Jasper"
DEFAULT_PIPER_VOICE = "en_US-lessac-medium"  # balanced size/quality
_NEUTTS_SAMPLES = Path(__file__).parent / "neutts_samples"


# Provider name -> the cache it populates (warm/release in tts_tool_lifecycle; a new local engine
# adds a row here plus a loader in _local_tts_warmers()). Piper keyed on absolute .onnx path
# (+cuda flag); KittenTTS on resolved ONNX and voices paths.
_piper_voice_cache: Dict[str, Any] = {}
_kittentts_model_cache: Dict[str, Any] = {}
_LOCAL_TTS_MODEL_CACHES: Dict[str, Dict[str, Any]] = {
    "piper": _piper_voice_cache, "kittentts": _kittentts_model_cache}




def _run_helper(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=timeout, stdin=subprocess.DEVNULL, env=env,
    )

# --- NeuTTS (subprocess via tools/neutts_synth.py so the ~500MB model exits after use) ---
def _generate_neutts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    neutts_config = tts_config.get("neutts") or {}
    _resolve_neutts_assets(neutts_config.get("model") or "neuphonic/neutts-air-q4-gguf")
    wav_path = _wav_sidecar_path(output_path)
    cmd = [
        sys.executable, str(Path(__file__).parent / "neutts_synth.py"),
        "--text", text,
        "--out", wav_path,
        "--ref-audio", neutts_config.get("ref_audio", "") or str(_NEUTTS_SAMPLES / "jo.wav"),
        "--ref-text", neutts_config.get("ref_text", "") or str(_NEUTTS_SAMPLES / "jo.txt"),
        "--model", neutts_config.get("model") or "neuphonic/neutts-air-q4-gguf",
        "--device", neutts_config.get("device", "cpu")]
    result = _run_helper(cmd, 120)
    if result.returncode != 0:  # the synth script reports success lines as "OK:" on stderr too
        error_lines = [l for l in result.stderr.strip().splitlines() if not l.startswith("OK:")]
        raise RuntimeError(f"NeuTTS synthesis failed: {chr(10).join(error_lines) or 'unknown error'}")
    return _finalize_wav_output(wav_path, output_path)


# --- Piper (local neural VITS, 44 languages) ---
def _get_piper_voices_dir() -> Path:
    """``<HERMES_HOME>/cache/piper-voices/`` holds user-provisioned voice assets."""
    from mercury_constants import get_hermes_dir
    root = Path(get_hermes_dir("cache/piper-voices", "piper_voices_cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_piper_voice_path(voice: str, download_dir: Path) -> str:
    """Resolve a local ONNX voice and its config; never fetch missing assets."""
    voice = voice or DEFAULT_PIPER_VOICE
    candidate = Path(voice).expanduser()
    model = candidate if candidate.suffix.lower() == ".onnx" else download_dir / f"{voice}.onnx"
    config = Path(f"{model}.json")
    if model.is_file() and config.is_file():
        return str(model.absolute())  # Preserve the validated .onnx.json sidecar beside symlinks.
    raise RuntimeError(
        f"Piper local voice assets missing: {model} and {config} are required. "
        "Set tts.piper.voice to an existing .onnx file with its .onnx.json sidecar, "
        "or place both files in tts.piper.voices_dir. Mercury does not download model weights.")


def _load_piper_voice_for_config(tts_config: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Resolve + load (or fetch from cache) the selected Piper voice -> ``(voice, piper_config)``.
    Shared by synthesis and ``warm_tts_provider`` so a warm-up fills exactly the slot synthesis hits."""
    piper_config = _section(tts_config, "piper")
    voice_name = piper_config.get("voice") or DEFAULT_PIPER_VOICE
    download_dir = Path(piper_config.get("voices_dir") or _get_piper_voices_dir()).expanduser()
    download_dir.mkdir(parents=True, exist_ok=True)
    use_cuda = bool(piper_config.get("use_cuda", False))
    model_path = _resolve_piper_voice_path(voice_name, download_dir)
    PiperVoice = _origin()._import_piper()

    def _load_piper_voice():
        logger.info("[Piper] Loading voice: %s", model_path)
        v = PiperVoice.load(model_path, use_cuda=use_cuda)
        logger.info("[Piper] Voice loaded")
        return v

    # speaker_id is applied per call via syn_config, so one instance serves every speaker.
    cache_key = f"{model_path}::cuda={use_cuda}"
    return _origin()._tts_cache_get_or_load(_piper_voice_cache, cache_key, _load_piper_voice), piper_config


_PIPER_ADVANCED_KNOBS = ("length_scale", "noise_scale", "noise_w_scale", "volume", "normalize_audio", "speaker_id")


def _generate_piper_tts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    import wave
    voice, piper_config = _load_piper_voice_for_config(tts_config)
    # Bad speaker_id drops to 0 (Piper's default); bools are rejected (they'd coerce to 1/0).
    _raw_speaker = piper_config.get("speaker_id", 0)
    speaker_id = _raw_speaker if type(_raw_speaker) is int else 0
    # Only build a SynthesisConfig when an advanced knob is configured, so we don't depend on a
    # newer piper-tts than the user's unless we must.
    syn_config = None
    if any(k in piper_config for k in _PIPER_ADVANCED_KNOBS):
        try:
            from piper import SynthesisConfig  # type: ignore
            syn_config = SynthesisConfig(
                length_scale=float(piper_config.get("length_scale", 1.0)),
                noise_scale=float(piper_config.get("noise_scale", 0.667)),
                noise_w_scale=float(piper_config.get("noise_w_scale", 0.8)),
                volume=float(piper_config.get("volume", 1.0)),
                normalize_audio=bool(piper_config.get("normalize_audio", True)),
                speaker_id=speaker_id)
        except ImportError:
            logger.warning("[Piper] SynthesisConfig not available in this piper-tts version — advanced knobs ignored")
    wav_path = _wav_sidecar_path(output_path)
    with wave.open(wav_path, "wb") as wav_file:
        if syn_config is not None:
            voice.synthesize_wav(text, wav_file, syn_config=syn_config)
        else:
            voice.synthesize_wav(text, wav_file)
    return _finalize_wav_output(wav_path, output_path)


# --- KittenTTS (local ONNX, 25-80MB models, CPU only) ---
def _resolve_kittentts_assets(model_name: str) -> Tuple[Path, Path, Dict[str, Any]]:
    """Read the SDK's config and both assets locally, bypassing its HF downloader."""
    directory = Path(model_name).expanduser()
    repo = model_name if "/" in model_name else f"KittenML/{model_name}"
    config_path = directory / "config.json" if directory.is_dir() else _cached_hf_file(repo, "config.json")
    hint = (
        "KittenTTS local assets missing. Set tts.kittentts.model to a directory containing "
        "config.json and its model_file (ONNX) and voices (NPZ), or provision the complete "
        f"existing Hugging Face cache for {repo}. Mercury does not download model weights.")
    if config_path is None or not config_path.is_file():
        raise RuntimeError(hint)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("type") not in ("ONNX1", "ONNX2"):
        raise RuntimeError("KittenTTS local config.json has an unsupported model type.")
    assets = []
    for key in ("model_file", "voices"):
        filename = config.get(key)
        if not isinstance(filename, str) or not filename:
            raise RuntimeError(f"{hint} Missing {key} in config.json.")
        path = directory / filename if directory.is_dir() else _cached_hf_file(repo, filename)
        if path is None or not path.is_file():
            raise RuntimeError(f"{hint} Missing {filename}.")
        assets.append(path.resolve())
    return assets[0], assets[1], config


def _load_kittentts_model_for_config(tts_config: Dict[str, Any]) -> Tuple[Any, Dict[str, Any]]:
    """Load the local ONNX implementation, never the SDK's downloading constructor."""
    kt_config = _section(tts_config, "kittentts")
    model_name = kt_config.get("model") or DEFAULT_KITTENTTS_MODEL
    model_path, voices_path, config = _resolve_kittentts_assets(model_name)
    KittenTTS = _origin()._import_kittentts()
    key = f"{model_path}::{voices_path}"

    def _load_kittentts_model():
        return KittenTTS(
            model_path=str(model_path), voices_path=str(voices_path),
            speed_priors=config.get("speed_priors", {}), voice_aliases=config.get("voice_aliases", {}))

    return _origin()._tts_cache_get_or_load(_kittentts_model_cache, key, _load_kittentts_model), kt_config


def _generate_kittentts(text: str, output_path: str, tts_config: Dict[str, Any]) -> str:
    model, kt_config = _load_kittentts_model_for_config(tts_config)
    audio = model.generate(  # numpy array at 24kHz
        text, voice=kt_config.get("voice", DEFAULT_KITTENTTS_VOICE),
        speed=kt_config.get("speed", 1.0), clean_text=kt_config.get("clean_text", True))
    import soundfile as sf
    wav_path = _wav_sidecar_path(output_path)
    sf.write(wav_path, audio, 24000)
    return _finalize_wav_output(wav_path, output_path)
