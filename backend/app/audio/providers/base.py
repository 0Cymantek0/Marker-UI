"""Provider-adapter seam for speech-to-text (plan §5.1).

The long-term direction is multi-provider STT (local faster-whisper, local
WhisperX, OpenAI, Groq, Deepgram, AssemblyAI, Azure). Adding a provider must
mean writing one adapter, not threading provider-specific logic through the
converter or route layer. This module lays that seam now.

``AudioTranscriptionProvider`` is a structural :class:`typing.Protocol`: any
object exposing ``transcribe(...) -> RawTranscript`` and an ``id`` satisfies it,
so a new provider is one class, not a branch in ``AudioConverter``.

``RawTranscript`` is the provider-agnostic shape every adapter must produce —
language, duration, segments with timestamps/text/confidence/words, plus a
``provider_metadata`` bag that preserves provider-specific raw diagnostics
(no_speech_prob, avg_logprob, compression_ratio, speaker fields, etc.) for
reproducibility. The normalizer (:mod:`app.audio.pipeline`) maps this into
Marker's auditable :class:`~app.audio.pipeline.AudioTranscript`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class RawWord:
    """A single word with timing and optional confidence/speaker."""

    word: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker: str | None = None
    speaker_confidence: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawSegment:
    """A provider-agnostic raw transcript segment before normalization."""

    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None
    speaker: str | None = None
    speaker_confidence: float | None = None
    no_speech_probability: float | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    overlap_warning: bool = False
    words: list[RawWord] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawTranscript:
    """The shape every STT adapter must emit.

    ``provider_metadata`` is the escape hatch for provider-specific diagnostics
    that have no slot above (e.g. Deepgram ``model_uuid``, OpenAI ``segments``
    verbosity). It is preserved verbatim into job metadata so a result stays
    reproducible and diagnosable regardless of which provider produced it.
    """

    language: str | None
    duration_ms: int
    model: str | None
    provider: str
    segments: list[RawSegment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize into the dict shape ``normalize_transcript`` consumes."""

        return {
            "language": self.language,
            "duration": self.duration_ms / 1000.0,
            "model": self.model,
            "provider": self.provider,
            "segments": [self._segment_to_dict(segment) for segment in self.segments],
            "warnings": list(self.warnings),
            "provider_metadata": dict(self.provider_metadata),
        }

    @classmethod
    def from_provider_dict(cls, payload: dict[str, Any]) -> RawTranscript:
        """Build a :class:`RawTranscript` from a flat provider-output dict.

        Adapters that already shape their native output into the
        ``{language, duration, segments, ...}`` dict form (faster-whisper's
        legacy in-converter shape) can hand it here instead of constructing
        :class:`RawSegment`/:class:`RawWord` objects by hand. Tolerant by
        design: a missing/odd field degrades to ``None``/empty, never raises.
        """

        raw_segments = payload.get("segments") or []
        segments: list[RawSegment] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            segments.append(
                RawSegment(
                    start_ms=max(0, int(round(_coerce_float(item.get("start")) * 1000))),
                    end_ms=max(0, int(round(_coerce_float(item.get("end")) * 1000))),
                    text=str(item.get("text") or "").strip(),
                    confidence=_coerce_float(item.get("confidence")),
                    speaker=item.get("speaker"),
                    speaker_confidence=_coerce_float(item.get("speaker_confidence")),
                    no_speech_probability=_coerce_float(item.get("no_speech_prob")),
                    avg_logprob=_coerce_float(item.get("avg_logprob")),
                    compression_ratio=_coerce_float(item.get("compression_ratio")),
                    overlap_warning=bool(item.get("overlap_warning") or False),
                    words=_raw_words(item.get("words")),
                )
            )
        return cls(
            language=payload.get("language"),
            duration_ms=max(0, int(round(_coerce_float(payload.get("duration")) * 1000))),
            model=payload.get("model"),
            provider=str(payload.get("provider") or "local_faster_whisper"),
            segments=segments,
            warnings=list(payload.get("warnings") or []),
            provider_metadata=dict(payload.get("provider_metadata") or {}),
        )

    @staticmethod
    def _segment_to_dict(segment: RawSegment) -> dict[str, Any]:
        return {
            "start": segment.start_ms / 1000.0,
            "end": segment.end_ms / 1000.0,
            "text": segment.text,
            "confidence": segment.confidence,
            "speaker": segment.speaker,
            "speaker_confidence": segment.speaker_confidence,
            "no_speech_prob": segment.no_speech_probability,
            "avg_logprob": segment.avg_logprob,
            "compression_ratio": segment.compression_ratio,
            "overlap_warning": segment.overlap_warning,
            "words": [_word_to_dict(word) for word in segment.words],
        }


def _word_to_dict(word: RawWord) -> dict[str, Any]:
    return {
        "word": word.word,
        "start": word.start_ms / 1000.0,
        "end": word.end_ms / 1000.0,
        "confidence": word.confidence,
        "speaker": word.speaker,
        "speaker_confidence": word.speaker_confidence,
    }


def _coerce_float(value: Any) -> float | None:
    """Best-effort float coercion for tolerant provider-dict parsing.

    A provider dict may carry ``None``, a non-numeric string, or a number where
    a float is expected; this returns ``None`` for anything that isn't a real
    number rather than raising, so a malformed field degrades gracefully.
    """

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/inf come back from some providers; treat them as missing.
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _raw_words(raw: Any) -> list[RawWord]:
    """Build RawWord objects from a provider-dict ``words`` list.

    Mirrors the tolerant parsing of :meth:`RawTranscript.from_provider_dict`:
    non-dict entries are skipped, odd fields degrade to ``None``/defaults.
    """

    if not isinstance(raw, (list, tuple)):
        return []
    words: list[RawWord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or "").strip()
        if not text:
            continue
        words.append(
            RawWord(
                word=text,
                start_ms=max(0, int(round(_coerce_float(item.get("start")) or 0.0) * 1000)),
                end_ms=max(0, int(round(_coerce_float(item.get("end")) or 0.0) * 1000)),
                confidence=_coerce_float(item.get("confidence")),
                speaker=item.get("speaker"),
                speaker_confidence=_coerce_float(item.get("speaker_confidence")),
            )
        )
    return words


@runtime_checkable
class AudioTranscriptionProvider(Protocol):
    """Contract every STT provider adapter must satisfy.

    Adapters transcribe one local audio file path. They must **never raise** on
    a transcription-quality problem — a hard failure (model not found, network,
    decode) is the only acceptable reason to raise, and the converter wraps the
    call so one bad file never aborts a batch. The capability matrix decides
    which UI controls apply; adapters just honor the config they receive.
    """

    id: str

    def transcribe(
        self,
        filepath: str,
        config: dict[str, Any],
        *,
        device: str | None = None,
        vocabulary_prompt: str | None = None,
    ) -> RawTranscript:
        """Transcribe *filepath* into a normalized :class:`RawTranscript`."""
        ...
