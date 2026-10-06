#!/usr/bin/env python3
"""Standalone NeuTTS synthesis helper.

Called by tts_tool.py via subprocess to keep the TTS model (~500MB)
in a separate process that exits after synthesis — no lingering memory.

Usage:
    python -m tools.neutts_synth --text "Hello" --out output.wav \
        --ref-audio samples/jo.wav --ref-text samples/jo.txt

Requires: python -m pip install -U neutts[all]
System:   apt install espeak-ng  (or brew install espeak-ng)
"""

import argparse
import json
import os
import struct
import sys
from pathlib import Path


def _cached_hf_snapshot(repo: str) -> Path | None:
    """Resolve the existing default-revision hub cache using only filesystem reads."""
    hf_home = Path(os.environ.get("HF_HOME") or
                   Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "huggingface")
    cache_dir = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE") or str(hf_home / "hub")
    root = Path(cache_dir) / ("models--" + repo.replace("/", "--"))
    ref = root / "refs" / "main"
    if not ref.is_file():
        return None
    revision = ref.read_text(encoding="utf-8").strip()
    if not revision or "/" in revision or "\\" in revision or revision in (".", ".."):
        return None
    snapshot = root / "snapshots" / revision
    return snapshot if snapshot.is_dir() else None


def _cached_hf_file(repo: str, filename: str) -> Path | None:
    """Look up an existing cache file without importing any downloading SDK."""
    snapshot = _cached_hf_snapshot(repo)
    result = snapshot / filename if snapshot is not None else None
    return result if result is not None and result.is_file() else None


def _require_transformer_assets(directory: Path, label: str) -> None:
    """Validate weights (including shards) before a transformers initializer runs."""
    if not (directory / "config.json").is_file():
        raise RuntimeError(f"{label}: config.json missing")
    if any((directory / name).is_file() for name in ("model.safetensors", "pytorch_model.bin")):
        return
    for name in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
        index = directory / name
        if index.is_file():
            shards = json.loads(index.read_text(encoding="utf-8")).get("weight_map", {})
            if shards and all((directory / shard).is_file() for shard in set(shards.values())):
                return
    raise RuntimeError(f"{label}: complete model weights missing")


def _resolve_neutts_assets(model: str) -> str:
    """Preflight backbone, codec and its semantic encoder; no SDK or hub requests."""
    hint = (
        "NeuTTS local assets unavailable. Set tts.neutts.model to an existing GGUF file "
        "or provision the complete backbone cache; the existing Hugging Face caches for "
        "neuphonic/neucodec (config.json, pytorch_model.bin, meta.yaml) and "
        "facebook/w2v-bert-2.0 (config, preprocessor and weights) are also required. "
        "Mercury does not download model weights.")
    try:
        candidate = Path(model).expanduser()
        if candidate.suffix.lower() == ".gguf":
            if not candidate.is_file():
                raise RuntimeError(f"backbone file missing: {candidate}")
            backbone = str(candidate.absolute())
        elif model.lower().endswith("gguf"):
            snapshot = _cached_hf_snapshot(model)
            files = sorted(snapshot.glob("*.gguf")) if snapshot is not None else []
            files = [path for path in files if path.is_file()]
            if len(files) != 1:
                raise RuntimeError(f"{model}: one cached GGUF backbone required")
            # Keep the .gguf symlink name: NeuTTS selects llama.cpp by extension.
            backbone = str(files[0].absolute())
        else:
            directory = candidate if candidate.is_dir() else _cached_hf_snapshot(model)
            if directory is None:
                raise RuntimeError(f"{model}: backbone cache missing")
            _require_transformer_assets(directory, model)
            if not (directory / "tokenizer_config.json").is_file() or not any(
                    (directory / name).is_file() for name in ("tokenizer.json", "tokenizer.model")):
                raise RuntimeError(f"{model}: tokenizer assets missing")
            backbone = str(candidate.resolve()) if candidate.is_dir() else model
        for filename in ("config.json", "pytorch_model.bin", "meta.yaml"):
            if _cached_hf_file("neuphonic/neucodec", filename) is None:
                raise RuntimeError(f"neuphonic/neucodec: {filename} missing")
        semantic = _cached_hf_snapshot("facebook/w2v-bert-2.0")
        if semantic is None:
            raise RuntimeError("facebook/w2v-bert-2.0: semantic encoder cache missing")
        _require_transformer_assets(semantic, "facebook/w2v-bert-2.0")
        if not (semantic / "preprocessor_config.json").is_file():
            raise RuntimeError("facebook/w2v-bert-2.0: preprocessor_config.json missing")
        return backbone
    except (RuntimeError, OSError, ValueError) as exc:
        raise RuntimeError(f"{hint} {exc}") from exc


def _write_wav(path: str, samples, sample_rate: int = 24000) -> None:
    """Write a WAV file from float32 samples (no soundfile dependency)."""
    import numpy as np

    if not isinstance(samples, np.ndarray):
        samples = np.array(samples, dtype=np.float32)
    samples = samples.flatten()

    # Clamp and convert to int16
    samples = np.clip(samples, -1.0, 1.0)
    pcm = (samples * 32767).astype(np.int16)

    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
    block_align = num_channels * (bits_per_sample // 8)
    data_size = len(pcm) * (bits_per_sample // 8)

    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, num_channels, sample_rate,
                            byte_rate, block_align, bits_per_sample))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm.tobytes())


def main():
    parser = argparse.ArgumentParser(description="NeuTTS synthesis helper")
    parser.add_argument("--text", required=True, help="Text to synthesize")
    parser.add_argument("--out", required=True, help="Output WAV path")
    parser.add_argument("--ref-audio", required=True, help="Reference voice audio path")
    parser.add_argument("--ref-text", required=True, help="Reference voice transcript path")
    parser.add_argument("--model", default="neuphonic/neutts-air-q4-gguf",
                        help="Existing local backbone path or fully cached model repo")
    parser.add_argument("--device", default="cpu", help="Device (cpu/cuda/mps)")
    args = parser.parse_args()

    # Set before importing the SDK: its codec and nested semantic encoder otherwise
    # fetch weights even when the backbone is an explicit local file.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        model = _resolve_neutts_assets(args.model)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # llama_cpp (backbone) offloads to GPU only for the literal string "gpu";
    # torch (codec) only accepts "cuda". A single --device value can't satisfy
    # both — "cuda" silently no-ops on the backbone, leaving it on CPU.
    backbone_device = "gpu" if args.device == "cuda" else args.device
    codec_device = args.device

    # Validate inputs
    ref_audio = Path(args.ref_audio).expanduser()
    ref_text_path = Path(args.ref_text).expanduser()
    if not ref_audio.exists():
        print(f"Error: reference audio not found: {ref_audio}", file=sys.stderr)
        sys.exit(1)
    if not ref_text_path.exists():
        print(f"Error: reference text not found: {ref_text_path}", file=sys.stderr)
        sys.exit(1)

    ref_text = ref_text_path.read_text(encoding="utf-8").strip()

    # Import and run NeuTTS
    try:
        from neutts import NeuTTS
    except ImportError:
        print("Error: neutts not installed. Run: python -m pip install -U neutts[all]", file=sys.stderr)
        sys.exit(1)

    tts = NeuTTS(
        backbone_repo=model,
        backbone_device=backbone_device,
        codec_repo="neuphonic/neucodec",
        codec_device=codec_device,
        language=(getattr(sys.modules.get(NeuTTS.__module__), "BACKBONE_LANGUAGE_MAP", {})
                  .get(args.model, "en-us")) if Path(model).exists() else None,
    )
    ref_codes = tts.encode_reference(str(ref_audio))
    wav = tts.infer(args.text, ref_codes, ref_text)

    # Write output
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import soundfile as sf
        sf.write(str(out_path), wav, 24000)
    except ImportError:
        _write_wav(str(out_path), wav, 24000)

    print(f"OK: {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
