"""Local audio transcription converter.

The converter stays thin: it resolves the STT provider, transcribes through the
adapter seam, normalizes the result, then renders. Provider-specific wiring lives
in :mod:`app.audio.providers`; auditable segment/notes assembly lives in
:mod:`app.audio.pipeline`; vocabulary pack handling in
:mod:`app.audio.vocabulary`. This file wires them together for one file.

Local-first by default (plan §3.1). A cloud provider is only used when the user
explicitly opts in via ``audio_allow_cloud_stt`` — otherwise a cloud provider id
is rejected before any audio leaves the machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.audio.ingest import probe_audio
from app.audio.pipeline import (
    AudioTranscript,
    normalize_transcript,
    render_enhanced_markdown,
    render_transcript_markdown,
    slug_source_id,
)
from app.audio.providers import build_provider, get_capability
from app.audio.speakers import (
    apply_speaker_aliases,
    speaker_timeline,
    summarize_speakers,
)
from app.audio.vocabulary import (
    compile_vocabulary_prompt,
    resolve_vocabulary_terms,
    vocabulary_report,
)
from app.conversion.registry import BaseConverter
from app.conversion.result import UniversalConversionResult
from app.conversion.stream_info import StreamInfo


_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"})


class AudioConverter(BaseConverter):
    """Transcribe audio files through the provider-adapter seam."""

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
        provider_id = _resolve_provider(config)
        capability = get_capability(provider_id)
        media_info = probe_audio(filepath)

        vocabulary_terms = resolve_vocabulary_terms(config)
        vocabulary_prompt = compile_vocabulary_prompt(
            terms=vocabulary_terms, capability=capability
        )
        provider = build_provider(provider_id)
        raw = provider.transcribe(
            filepath,
            config,
            device=device,
            vocabulary_prompt=vocabulary_prompt if capability.supports_prompt_context else None,
        )
        raw_payload = raw.to_dict()
        raw_payload["media_info"] = media_info
        # Diarization is provider-specific (plan §10). A provider that can't
        # diarize surfaces an explicit warning instead of silently faking a single
        # speaker when the user asked for diarization.
        if config.get("audio_diarization") and not capability.supports_diarization:
            raw_payload.setdefault("warnings", []).append(
                "diarization_requested_but_unsupported_by_provider"
            )

        title = Path(filepath).stem
        source_label = str(config.get("audio_source_label") or Path(filepath).name)
        source_id = str(config.get("audio_source_id") or slug_source_id(source_label))
        transcript = normalize_transcript(
            raw_payload,
            source_label=source_label,
            source_id=source_id,
            config=config,
        )
        # Speaker identity is user-controlled (plan §10): only labels the user
        # explicitly mapped are renamed; everything else stays anonymous. Diarization
        # itself is provider-specific — faster-whisper can't, so the warning surfaced
        # above propagates through normalize into the transcript.
        transcript = apply_speaker_aliases(transcript, config.get("audio_speaker_aliases"))

        enhancement_plan = _resolve_enhancement_plan(config)
        output_mode = enhancement_plan["mode"]
        if output_mode == "transcript":
            text = render_transcript_markdown(transcript, title=title)
        else:
            text = render_enhanced_markdown(
                transcript,
                title=title,
                template=enhancement_plan["template"],
                context=config.get("audio_context"),
            )

        transcript_text = " ".join(segment.text for segment in transcript.segments)
        vocab_report = vocabulary_report(
            terms=vocabulary_terms,
            transcript_text=transcript_text,
            truncated=_vocabulary_was_truncated(vocabulary_terms, vocabulary_prompt),
            provider_prompted=bool(vocabulary_prompt and capability.supports_prompt_context),
        )
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
                    "provider_capability": {
                        "provider_id": capability.provider_id,
                        "cloud": capability.cloud,
                        "privacy_level": capability.privacy_level,
                        "supports_diarization": capability.supports_diarization,
                        "supports_word_timestamps": capability.supports_word_timestamps,
                        "supports_confidence": capability.supports_confidence,
                    },
                    "vocabulary": vocab_report,
                    "quality": _audio_quality(transcript),
                    "speakers": _speaker_metadata(transcript, config),
                    "enhancement": {
                        "mode": output_mode,
                        "template": enhancement_plan["template"],
                        "trigger": enhancement_plan["trigger"],
                        "text_enhancement_enabled": enhancement_plan["text_enabled"],
                        "text_enhancement_strength": enhancement_plan["text_strength"],
                        "structural_enhancement_enabled": enhancement_plan["structural_enabled"],
                        "structural_enhancement_mode": enhancement_plan["structural_mode"],
                        "provider": "local_deterministic" if output_mode != "transcript" else None,
                        "provenance_required": output_mode != "transcript",
                    },
                    "raw_provider_metadata": dict(raw.provider_metadata),
                },
            },
        )


def _resolve_provider(config: dict[str, Any]) -> str:
    """Resolve the audio provider id, enforcing cloud opt-in (plan §3.1).

    Cloud STT is never used unless ``audio_allow_cloud_stt`` is explicitly true.
    A cloud provider chosen without opt-in is rejected before transcription so no
    audio leaves the machine unexpectedly.
    """

    provider_id = str(config.get("audio_provider") or "local_faster_whisper").strip().lower()
    capability = get_capability(provider_id)
    if capability.cloud and not _truthy(config.get("audio_allow_cloud_stt")):
        raise PermissionError(
            f"Audio provider {provider_id!r} is cloud-based but cloud STT is not enabled. "
            "Enable 'allow cloud STT' in Advanced Audio settings to send audio to this provider."
        )
    return provider_id


def _resolve_output_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("audio_output_mode") or "transcript").lower()
    if mode in {"enhanced", "notes", "meeting_notes", "lecture_notes", "interview_qna", "action_decision_log"}:
        return mode
    return "transcript"


def _resolve_enhancement_plan(config: dict[str, Any]) -> dict[str, Any]:
    """Map UI enhancement toggles to the deterministic evidence-first renderer."""

    requested_mode = _resolve_output_mode(config)
    text_enabled = _truthy(config.get("audio_text_enhancement_enabled"))
    text_strength = _clamp_int(config.get("audio_text_enhancement_strength"), minimum=0, maximum=5)
    structural_enabled = _truthy(config.get("audio_structural_enhancement_enabled"))
    structural_mode = str(config.get("audio_structural_enhancement_mode") or "auto").strip().lower()

    if requested_mode != "transcript":
        return {
            "mode": requested_mode,
            "template": requested_mode,
            "trigger": "output_mode",
            "text_enabled": text_enabled,
            "text_strength": text_strength,
            "structural_enabled": structural_enabled,
            "structural_mode": structural_mode,
        }
    if structural_enabled:
        template = structural_mode if structural_mode and structural_mode != "auto" else "structured_notes"
        return {
            "mode": "enhanced",
            "template": template,
            "trigger": "structural_enhancement",
            "text_enabled": text_enabled,
            "text_strength": text_strength,
            "structural_enabled": True,
            "structural_mode": structural_mode,
        }
    if text_enabled:
        return {
            "mode": "enhanced",
            "template": "text_enhancement",
            "trigger": "text_enhancement",
            "text_enabled": True,
            "text_strength": max(1, text_strength),
            "structural_enabled": False,
            "structural_mode": structural_mode,
        }
    return {
        "mode": "transcript",
        "template": "transcript",
        "trigger": None,
        "text_enabled": text_enabled,
        "text_strength": text_strength,
        "structural_enabled": structural_enabled,
        "structural_mode": structural_mode,
    }


def _clamp_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, coerced))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _vocabulary_was_truncated(terms: list[str], prompt: str | None) -> bool:
    """True only when the vocabulary prompt was actually trimmed.

    Re-derived from the term list rather than threaded through the provider: if
    the full "Vocabulary terms: a, b, …" rendering would exceed the cap and the
    emitted prompt is shorter than that full rendering, the prompt was cut. Keeps
    the diagnostics truthful without changing the prompt compiler's signature.
    """

    if not prompt or not terms:
        return False
    full = "Vocabulary terms: " + ", ".join(terms)
    return len(prompt) < len(full)


def _audio_quality(transcript: AudioTranscript) -> dict[str, Any]:
    """Expose the enriched risk summary as the canonical audio-quality block.

    The heatmap (plan §8) reads this: mean confidence, low/unknown confidence
    counts, overlaps, long gaps, and a single ``review_required`` verdict so the
    UI can flag weak evidence without re-deriving it from segment warnings.
    """

    summary = dict(transcript.risk_summary)
    summary["warnings"] = list(transcript.warnings)
    return summary


def _speaker_metadata(transcript: AudioTranscript, config: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_speakers(transcript)
    aliases = config.get("audio_speaker_aliases")
    if aliases and isinstance(aliases, dict):
        summary["user_confirmed"] = True
        summary["aliases"] = dict(aliases)
    summary["timeline"] = speaker_timeline(transcript)
    return summary
