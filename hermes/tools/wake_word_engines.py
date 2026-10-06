"""Local-only wake-word engines; tools.wake_word owns configuration and dispatch.

This module is the single owner of engine initialization. Model assets must be
provisioned locally; detection never downloads model weights.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.wake_word")


def _ww():
    from tools import wake_word
    return wake_word


class _Engine:
    """Minimal hotword-engine contract: feed int16 frames, get a bool."""

    frame_length: int = 1280  # 80 ms at 16 kHz

    #: Optional (matched phrase, profile name) of the most recent fire.
    #: Multi-phrase engines (sherpa) set this for profile routing; the
    #: single-phrase engines leave it None (callers fall back to the
    #: configured phrase / active profile).
    last_match: Optional[tuple[str, str]] = None

    def process(self, frame) -> bool:  # frame: 1-D int16 ndarray
        raise NotImplementedError

    def reset(self) -> None:
        """Clear any internal audio/feature buffer (called on every (re)start)."""
        pass

    def close(self) -> None:
        pass


def _looks_like_path(value: str) -> bool:
    return (
        os.sep in value
        or value.endswith((".onnx", ".tflite", ".ppn"))
        or os.path.exists(value)
    )


class _OpenWakeWordEngine(_Engine):
    """openWakeWord — free, local ONNX/TFLite hotword detection."""

    # openWakeWord recommends 80 ms frames (1280 samples) for efficiency.
    frame_length = 1280

    def __init__(self, cfg: Dict[str, Any]):
        from tools import lazy_deps

        lazy_deps.ensure("wake.openwakeword", prompt=False)

        import openwakeword
        from openwakeword.model import Model

        sub = cfg.get("openwakeword") if isinstance(cfg.get("openwakeword"), dict) else {}
        model_ref = str(sub.get("model") or _ww()._BUNDLED_MODEL_NAME).strip()
        framework = _ww().resolve_inference_framework(cfg)
        # openWakeWord returns a 0..1 score per frame; sensitivity IS the raw
        # threshold a score must clear. Higher = stricter (fewer false fires).
        # Default 0.6 sits above openWakeWord's permissive 0.5 baseline, which
        # let near-misses like "hey hor" through.
        self._threshold = _ww()._sensitivity(cfg)
        self._confirm_needed = _ww()._confirmation_frames(cfg)
        self._confirm_streak = 0

        # openWakeWord silently downgrades tflite -> onnx when no tflite runtime
        # imports (model.py). On macOS ARM64 that lands on the backend whose
        # embedding model is broken, so the listener would arm and never fire.
        # Install + bridge the runtime first, and refuse the downgrade rather
        # than ship a dead ear.
        if framework == "tflite" and not _ww().ensure_tflite_runtime():
            # Same lazy-install contract as every other backend; the platform
            # gate lives here because dep specs can't carry PEP 508 markers.
            try:
                lazy_deps.ensure("wake.openwakeword.tflite", prompt=False)
            except Exception as e:
                logger.debug("wake word: tflite runtime install failed: %s", e)
            if not _ww().ensure_tflite_runtime():
                if _ww()._is_macos_arm64():
                    raise RuntimeError(
                        "The wake word needs the tflite backend on this Mac, but its "
                        "runtime is missing. Install it with: pip install ai-edge-litert"
                    )
                logger.warning("wake word: no tflite runtime available — falling back to onnx")
                framework = "onnx"

        # Default (or explicit "hey_hermes") → the bundled model; a built-in name
        # or custom path is used as-is.
        if model_ref.lower() in _ww()._BUNDLED_MODEL_ALIASES:
            model_ref = _ww()._bundled_wakeword_path(framework)

        hint = (
            "Provision local openWakeWord classifier and shared feature models; "
            "set wake_word.openwakeword.model to a local model file. "
            "Mercury never downloads wake-word model weights."
        )
        if not _looks_like_path(model_ref):
            metadata = openwakeword.models.get(model_ref, {})
            cached = metadata.get("model_path")
            if not cached:
                raise RuntimeError(f"Local wake-word model {model_ref!r} is unavailable. {hint}")
            model_ref = str(Path(cached).with_suffix(f".{framework}"))
        model_ref = str(Path(model_ref).expanduser())
        _require_local_file(Path(model_ref), hint)
        features = {}
        for name, parameter in (
            ("melspectrogram", "melspec_model_path"),
            ("embedding", "embedding_model_path"),
        ):
            cached = openwakeword.FEATURE_MODELS.get(name, {}).get("model_path")
            if not cached:
                raise RuntimeError(f"Local openWakeWord {name} feature model is unavailable. {hint}")
            path = Path(cached).with_suffix(f".{framework}")
            _require_local_file(path, hint)
            features[parameter] = str(path)
        self._model = Model(
            wakeword_models=[model_ref], inference_framework=framework, **features
        )
        self._labels = list(self._model.models.keys())

    def process(self, frame) -> bool:
        scores = self._model.predict(frame)
        over = any(score >= self._threshold for score in scores.values())
        # Require N consecutive over-threshold frames: a real phrase holds the
        # score high across frames, a stray ambient phoneme spikes just one.
        if over:
            self._confirm_streak += 1
            if self._confirm_streak >= self._confirm_needed:
                self._confirm_streak = 0
                return True
            return False
        self._confirm_streak = 0
        return False

    def reset(self) -> None:
        # Clears openWakeWord's rolling feature/prediction buffer so stale audio
        # captured before a pause can't re-fire the moment we resume.
        self._confirm_streak = 0
        try:
            self._model.reset()
        except Exception:
            pass

    def close(self) -> None:
        self.reset()


# sherpa-onnx open-vocabulary KWS model: a locally provisioned English
# streaming zipformer transducer. Keywords are tokenized at runtime.
_SHERPA_KWS_MODEL_DIR = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"


def _sherpa_model_root() -> Path:
    from mercury_constants import get_hermes_home

    return get_hermes_home() / "cache" / "wakewords"


def _require_local_file(path: Path, hint: str) -> None:
    """Reject missing, empty, unreadable or non-file assets before SDK initialization."""
    try:
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(1):
                    return
    except OSError:
        pass
    raise RuntimeError(f"Required local model file is missing, empty or unreadable: {path}. {hint}")


def _sherpa_model_files(model_dir: str = "") -> tuple[Path, Dict[str, str]]:
    d = Path(model_dir).expanduser() if model_dir else _sherpa_model_root() / _SHERPA_KWS_MODEL_DIR
    hint = (
        f"Provision a complete local sherpa KWS model at {d}, or set "
        "wake_word.sherpa.model_dir to its directory (tokens.txt, bpe.model, "
        "encoder, decoder and joiner ONNX files). "
        "Mercury never downloads wake-word model weights."
    )
    files = {"tokens": str(d / "tokens.txt"), "bpe_model": str(d / "bpe.model")}
    for name in ("tokens", "bpe_model"):
        _require_local_file(Path(files[name]), hint)
    for part in ("encoder", "decoder", "joiner"):
        pattern = f"{part}-*[!8].onnx"
        hits = sorted(d.glob(pattern))
        selected = None
        for path in hits:
            try:
                _require_local_file(path, hint)
            except RuntimeError:
                continue
            selected = str(path)
            break
        if selected is None:
            raise RuntimeError(f"Required local sherpa model file missing: {d}/{pattern}. {hint}")
        files[part] = selected
    return d, files


class _SherpaKwsEngine(_Engine):
    """sherpa-onnx open-vocabulary keyword spotting — any typed phrase, zero training.

    The configured ``wake_word.phrase`` is BPE-tokenized at runtime against the
    model's vocabulary, so "hey mercury", "hey coder", or any other phrase works
    immediately. Here ``phrase`` is DETECTION config, not a cosmetic label.
    """

    # sherpa's streaming zipformer consumes arbitrary chunk sizes; 1280
    # samples (80 ms) matches the shared capture path.
    frame_length = 1280

    def __init__(self, cfg: Dict[str, Any]):
        from tools import lazy_deps

        lazy_deps.ensure("wake.sherpa", prompt=False)

        import sherpa_onnx
        from sherpa_onnx import text2token

        sub = cfg.get("sherpa") if isinstance(cfg.get("sherpa"), dict) else {}
        model_dir = str(sub.get("model_dir") or "").strip()
        d, model_files = _sherpa_model_files(model_dir)

        # Phrase set: this profile's own phrase, plus — when profile routing is
        # on — every other wake-enabled profile's phrase, so ONE listener can
        # wake any profile ("hey mercury" / "hey coder" / ...). display-name →
        # profile is kept for routing the match back.
        phrase = str(_ww()._get(cfg, "phrase") or "hey mercury").strip()
        own_profile = _ww()._active_profile_name()
        phrase_map: Dict[str, str] = {phrase: own_profile}
        if bool(cfg.get("profile_routing", True)):
            for prof, p in _ww().enrolled_profile_phrases().items():
                phrase_map.setdefault(p.strip(), prof)

        phrases = list(phrase_map)
        # Runtime tokenization of the arbitrary phrases — the open-vocab core.
        tokens = text2token(
            [p.upper() for p in phrases],
            tokens=str(d / "tokens.txt"),
            tokens_type="bpe",
            bpe_model=str(d / "bpe.model"),
        )
        import tempfile

        # sherpa keyword entries reject spaces in the @display-name; underscore
        # them and map display → profile for match routing.
        self._display_to_profile: Dict[str, str] = {}
        kw = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="mercury-kws-", delete=False, encoding="utf-8"
        )
        for p, toks in zip(phrases, tokens):
            display = p.upper().replace(" ", "_")
            self._display_to_profile[display] = phrase_map[p]
            kw.write(" ".join(toks) + f" @{display}\n")
        kw.close()
        self._keywords_file = kw.name
        #: (phrase display name, profile) of the most recent fire, for routing.
        self.last_match: Optional[tuple[str, str]] = None

        # Map the shared 0..1 sensitivity onto sherpa's keywords_threshold.
        # 0.5 lands exactly on sherpa's recommended default (0.25); live TTS
        # matrix testing showed our previous stricter mapping (0.35) missed
        # ~12% of true positives while 0.25 held zero false fires.
        threshold = 0.05 + 0.4 * _ww()._sensitivity(cfg)

        # All model assets were admitted before tokenization or SDK initialization.

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=model_files["tokens"],
            encoder=model_files["encoder"],
            decoder=model_files["decoder"],
            joiner=model_files["joiner"],
            keywords_file=self._keywords_file,
            keywords_threshold=threshold,
            num_threads=1,
        )
        self._stream = self._spotter.create_stream()

    def process(self, frame) -> bool:
        import numpy as np

        samples = np.asarray(frame, dtype=np.float32) / 32768.0
        self._stream.accept_waveform(_ww().SAMPLE_RATE, samples)
        fired = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                fired = True
                display = str(result)
                self.last_match = (
                    display.replace("_", " ").lower(),
                    self._display_to_profile.get(display, ""),
                )
                # Reset decoder state so one utterance can't fire repeatedly.
                self._spotter.reset_stream(self._stream)
        return fired

    def reset(self) -> None:
        # Fresh stream drops all buffered audio/decoder state (pause → resume
        # must not re-fire on stale audio).
        try:
            self._stream = self._spotter.create_stream()
        except Exception:
            pass

    def close(self) -> None:
        try:
            os.unlink(self._keywords_file)
        except OSError:
            pass


class _PorcupineEngine(_Engine):
    """Picovoice Porcupine — premium, on-device, needs an access key."""

    def __init__(self, cfg: Dict[str, Any]):
        from tools import lazy_deps

        lazy_deps.ensure("wake.porcupine", prompt=False)

        import pvporcupine

        access_key = (os.getenv("PORCUPINE_ACCESS_KEY") or "").strip()
        if not access_key:
            raise RuntimeError(
                "Porcupine wake word requires PORCUPINE_ACCESS_KEY "
                "(get a free key at https://console.picovoice.ai)."
            )

        sub = cfg.get("porcupine") if isinstance(cfg.get("porcupine"), dict) else {}
        keyword = str(sub.get("keyword") or "jarvis").strip()
        # Porcupine's `sensitivities` runs the OPPOSITE way to our shared knob:
        # per Picovoice, higher = more true positives AND more false alarms
        # (looser). Our config contract is "higher = stricter" everywhere, so
        # invert it here to keep one consistent meaning across all engines.
        porcupine_sensitivity = 1.0 - _ww()._sensitivity(cfg)

        kwargs: Dict[str, Any] = {"access_key": access_key, "sensitivities": [porcupine_sensitivity]}
        if _looks_like_path(keyword):
            kwargs["keyword_paths"] = [keyword]
        else:
            kwargs["keywords"] = [keyword]

        self._porcupine = pvporcupine.create(**kwargs)
        self.frame_length = self._porcupine.frame_length

    def process(self, frame) -> bool:
        # pvporcupine wants a plain list/sequence of int16 samples.
        return self._porcupine.process(frame) >= 0

    def close(self) -> None:
        try:
            self._porcupine.delete()
        except Exception:
            pass
