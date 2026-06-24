"""Local audio transcription converter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.audio.ingest import probe_audio
from app.audio.pipeline import (
    normalize_transcript,
    render_enhanced_markdown,
    render_transcript_markdown,
    slug_source_id,
)
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"})


class AudioConverter(BaseConverter):
    """Transcribe audio files locally with faster-whisper."""

    engine_name = "audio"
    priority = 10
    requires_marker_models = False
    requires_gpu = False

    @property
    def supported_extensions(self) -> frozenset[str]:
        return _AUDIO_EXTENSIONS

    def accepts(self, stream_info: StreamInfo, config: dict[str, Any]) -> bool:
        return stream_info.extension in _AUDIO_EXTENSIONS

    def convert(
        self,
        filepath: str,
        config: dict[str, Any],
        device: str | None = None,
    ) -> UniversalConversionResult:
        media_info = probe_audio(filepath)
        raw_transcript = _transcribe_audio(filepath, config, device=device)
        raw_transcript["media_info"] = media_info
        title = Path(filepath).stem
        source_label = str(config.get("audio_source_label") or Path(filepath).name)
        source_id = str(config.get("audio_source_id") or slug_source_id(source_label))
        transcript = normalize_transcript(
            raw_transcript,
            source_label=source_label,
            source_id=source_id,
            config=config,
        )
        output_mode = str(config.get("audio_output_mode") or "transcript").lower()
        if output_mode in {"enhanced", "notes", "meeting_notes", "lecture_notes"}:
            text = render_enhanced_markdown(
                transcript,
                title=title,
                template=output_mode,
                context=config.get("audio_context"),
            )
        else:
            output_mode = "transcript"
            text = render_transcript_markdown(transcript, title=title)

        return UniversalConversionResult(
            text=text,
            extension="md",
            metadata={
                "engine_detail": {
                    "format": Path(filepath).suffix.lower().lstrip("."),
                    "language": transcript.language,
                    "duration": transcript.duration_ms / 1000.0,
                    "segment_count": len(transcript.segments),
                    "model": transcript.model,
                    "output_mode": output_mode,
                    "provider": transcript.provider,
                    "warnings": list(transcript.warnings),
                    "risk_summary": dict(transcript.risk_summary),
                    "media_info": dict(transcript.media_info),
                },
                "audio": {
                    "transcript": transcript.to_dict(),
                    "enhancement": {
                        "mode": output_mode,
                        "provider": "local_deterministic" if output_mode != "transcript" else None,
                        "provenance_required": output_mode != "transcript",
                    },
                },
            },
        )


def _transcribe_audio(filepath: str, config: dict[str, Any], device: str | None = None) -> dict[str, Any]:
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
        initial_prompt=_audio_initial_prompt(config),
        word_timestamps=bool(config.get("audio_word_timestamps", False)),
    )
    segments = [
        {
            "start": float(segment.start),
            "end": float(segment.end),
            "text": str(segment.text).strip(),
            "confidence": _segment_confidence(segment),
            "words": _segment_words(segment),
        }
        for segment in segments_iter
    ]
    return {
        "language": getattr(info, "language", language),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
        "segments": segments,
        "model": model_name,
        "provider": "local_faster_whisper",
    }


def _audio_initial_prompt(config: dict[str, Any]) -> str | None:
    raw = config.get("audio_vocabulary") or config.get("audio_vocabulary_terms")
    if not raw:
        return None
    if isinstance(raw, str):
        terms = [part.strip() for part in raw.replace("\n", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        terms = [str(part).strip() for part in raw]
    else:
        terms = []
    terms = [term for term in terms if term]
    if not terms:
        return None
    return "Vocabulary terms: " + ", ".join(terms[:100])


def _segment_confidence(segment: Any) -> float | None:
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
