"""Local faster-whisper speech-to-text provider adapter.

This adapter is the *only* transcriber shipped today. It wraps the exact
behavior that previously lived inline in ``app.conversion.converters.audio``,
so migrating transcription behind a provider seam changes nothing for users:
same model resolution, same compute/device handling, same confidence
derivation, same vocabulary→``initial_prompt`` compilation.

The seam exists so cloud STT providers (OpenAI, Groq, Deepgram, AssemblyAI,
Azure) and an optional local WhisperX/pyannote diarization route can be added
later as sibling adapters behind the same
:class:`~app.audio.providers.base.AudioTranscriptionProvider` contract — see
the capability matrix in :mod:`app.audio.providers.capabilities`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.audio.providers.base import AudioTranscriptionProvider, RawTranscript

logger = logging.getLogger(__name__)


class FasterWhisperProvider:
    """CTranslate2-based Whisper implementation; Marker's local default.

    Satisfies the :class:`AudioTranscriptionProvider` protocol structurally —
    it exposes ``id`` and ``transcribe(...)`` with the right signature, so it
    does not need to inherit the Protocol explicitly.
    """

    id = "local_faster_whisper"

    def transcribe(
        self,
        filepath: str,
        config: dict[str, Any],
        *,
        device: str | None = None,
        vocabulary_prompt: str | None = None,
    ) -> RawTranscript:
        from faster_whisper import WhisperModel

        model_name = str(config.get("audio_model") or "tiny.en")
        model_device = str(config.get("audio_device") or device or "cpu")
        compute_type = str(config.get("audio_compute_type") or "int8")
        language = config.get("audio_language") or config.get("lang")
        model = WhisperModel(model_name, device=model_device, compute_type=compute_type)
        segments_iter, info = model.transcribe(
            filepath,
            language=language or None,
            beam_size=int(config.get("audio_beam_size", 5)),
            vad_filter=bool(config.get("audio_vad_filter", True)),
            initial_prompt=vocabulary_prompt,
            word_timestamps=bool(config.get("audio_word_timestamps", False)),
        )
        segments = [
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": str(segment.text).strip(),
                "confidence": _segment_confidence(segment),
                "no_speech_prob": _coerce_float(getattr(segment, "no_speech_prob", None)),
                "avg_logprob": _coerce_float(getattr(segment, "avg_logprob", None)),
                "compression_ratio": _coerce_float(getattr(segment, "compression_ratio", None)),
                "words": _segment_words(segment),
            }
            for segment in segments_iter
        ]
        return RawTranscript.from_provider_dict(
            {
                "language": getattr(info, "language", language),
                "duration": float(getattr(info, "duration", 0.0) or 0.0),
                "segments": segments,
                "model": model_name,
                "provider": self.id,
                "provider_metadata": {
                    "device": model_device,
                    "compute_type": compute_type,
                    "beam_size": int(config.get("audio_beam_size", 5)),
                    "vad_filter": bool(config.get("audio_vad_filter", True)),
                    "word_timestamps": bool(config.get("audio_word_timestamps", False)),
                },
            }
        )


def _segment_confidence(segment: Any) -> float | None:
    """Map faster-whisper diagnostics onto a 0..1 confidence score.

    ``no_speech_prob`` is the most direct speech-quality signal; when absent we
    fall back to ``avg_logprob`` rescaled from its roughly [-2, 0] working range.
    """
    no_speech_prob = getattr(segment, "no_speech_prob", None)
    if no_speech_prob is not None:
        try:
            return max(0.0, min(1.0, 1.0 - float(no_speech_prob)))
        except (TypeError, ValueError):
            return None
    avg_logprob = getattr(segment, "avg_logprob", None)
    if avg_logprob is not None:
        try:
            return max(0.0, min(1.0, (float(avg_logprob) + 2.0) / 2.0))
        except (TypeError, ValueError):
            return None
    return None


def _segment_words(segment: Any) -> list[dict[str, Any]]:
    words = getattr(segment, "words", None) or []
    normalized: list[dict[str, Any]] = []
    for word in words:
        raw_word = str(getattr(word, "word", "") or "").strip()
        if not raw_word:
            continue
        normalized.append(
            {
                "word": raw_word,
                "start": float(getattr(word, "start", 0.0) or 0.0),
                "end": float(getattr(word, "end", 0.0) or 0.0),
                "confidence": _coerce_float(getattr(word, "probability", None)),
            }
        )
    return normalized


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
