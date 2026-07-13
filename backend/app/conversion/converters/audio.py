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

from app.audio.pipeline import (
    AudioTranscript,
    append_contradiction_section,
    detect_possible_contradictions,
    render_enhanced_markdown,
    render_text_enhanced_markdown,
    render_transcript_markdown,
)
from app.audio.transcribe import transcribe_audio_file_detailed
from app.audio.speakers import (
    speaker_timeline,
    summarize_speakers,
)
from app.audio.vocabulary import (
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
        title = Path(filepath).stem
        source_label = str(config.get("audio_source_label") or Path(filepath).name)
        run = transcribe_audio_file_detailed(
            filepath,
            config,
            device=device,
            source_label=source_label,
            source_id=config.get("audio_source_id"),
        )
        transcript = run.transcript
        capability = run.capability

        enhancement_plan = _resolve_enhancement_plan(config)
        output_mode = enhancement_plan["mode"]
        if output_mode == "transcript":
            text = render_transcript_markdown(transcript, title=title)
        elif enhancement_plan["trigger"] == "text_enhancement" and not enhancement_plan["structural_enabled"]:
            text = render_text_enhanced_markdown(
                transcript,
                title=title,
                strength=enhancement_plan["text_strength"],
            )
        else:
            text = render_enhanced_markdown(
                transcript,
                title=title,
                template=enhancement_plan["template"],
                context=config.get("audio_context"),
            )
        provenance_validation = _validate_enhancement_provenance(
            text,
            transcript,
            require_source_refs=_truthy(config.get("audio_enhancement_require_source_refs", True)),
        )
        if (
            output_mode != "transcript"
            and not provenance_validation["valid"]
            and _truthy(config.get("audio_enhancement_fallback_on_validation_failure", True))
        ):
            text = render_transcript_markdown(transcript, title=title)
            output_mode = "transcript"
            enhancement_plan = {
                **enhancement_plan,
                "mode": "transcript",
                "trigger": "validation_fallback",
            }
            provenance_validation = {
                **_validate_enhancement_provenance(
                    text,
                    transcript,
                    require_source_refs=_truthy(config.get("audio_enhancement_require_source_refs", True)),
                ),
                "fallback_applied": True,
            }
        elif output_mode != "transcript" and not provenance_validation["valid"]:
            raise RuntimeError(
                "Audio enhancement failed provenance validation: "
                + ", ".join(provenance_validation["missing"])
            )
        contradictions = (
            detect_possible_contradictions(transcript)
            if _truthy(config.get("audio_contradiction_detection"))
            else []
        )
        if contradictions:
            text = append_contradiction_section(text, contradictions)

        transcript_text = " ".join(segment.text for segment in transcript.segments)
        vocab_report = vocabulary_report(
            terms=run.vocabulary_terms,
            transcript_text=transcript_text,
            truncated=_vocabulary_was_truncated(run.vocabulary_terms, run.vocabulary_prompt),
            provider_prompted=bool(run.vocabulary_prompt and capability.supports_prompt_context),
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
                    "contradictions": contradictions,
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
                        "source_refs_required": _truthy(config.get("audio_enhancement_require_source_refs", True)),
                        "source_refs_valid": provenance_validation["valid"],
                        "provenance_validation": provenance_validation,
                    },
                    "raw_provider_metadata": run.raw_provider_metadata,
                },
            },
        )


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


def _validate_enhancement_provenance(
    text: str,
    transcript: AudioTranscript,
    *,
    require_source_refs: bool,
) -> dict[str, Any]:
    """Verify enhanced output keeps direct citations for every spoken segment."""

    if not require_source_refs:
        return {"valid": True, "required": False, "missing": [], "fallback_applied": False}

    missing: list[str] = []
    for segment in transcript.segments:
        if not segment.text:
            continue
        if segment.segment_id not in text or segment.source_ref() not in text:
            missing.append(segment.segment_id)
    return {
        "valid": not missing,
        "required": True,
        "missing": missing,
        "fallback_applied": False,
    }


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
