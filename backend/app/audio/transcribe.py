"""Shared audio transcription entry point (plan §5.1).

The video converter demuxes audio from a video container and needs to run the
same provider-adapter transcription path as the audio converter, without
duplicating provider resolution, vocabulary compilation, or the raw→normalized
mapping. This module is the single shared seam: resolve provider, transcribe,
return a normalized :class:`AudioTranscript`.

Kept separate from :mod:`app.conversion.converters.audio` so importing it does
not pull the converter registry — ``video`` imports this, not the converter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.audio.ingest import probe_audio
from app.audio.pipeline import AudioTranscript, normalize_transcript, slug_source_id
from app.audio.providers import ProviderCapability, build_provider, get_capability
from app.audio.providers.registry import (
    validate_audio_benchmark_selection,
    validate_audio_diarization_selection,
    validate_audio_fusion_selection,
    validate_provider_selection,
)
from app.audio.speakers import apply_speaker_aliases
from app.audio.vocabulary import compile_vocabulary_prompt, resolve_vocabulary_terms


@dataclass(frozen=True)
class AudioTranscriptionRun:
    """Detailed shared transcription result for converters that need metadata."""

    transcript: AudioTranscript
    provider_id: str
    capability: ProviderCapability
    vocabulary_terms: list[str]
    vocabulary_prompt: str | None
    raw_provider_metadata: dict[str, Any]


def transcribe_audio_file(
    filepath: str,
    config: dict[str, Any],
    *,
    device: str | None = None,
    source_label: str | None = None,
    source_id: str | None = None,
) -> AudioTranscript:
    """Transcribe one audio file through the provider-adapter seam.

    Mirrors :meth:`AudioConverter.convert`'s transcription half — resolve the
    provider (enforcing cloud opt-in), compile vocabulary into a provider-aware
    prompt, transcribe, normalize into an auditable :class:`AudioTranscript`,
    and apply any user-confirmed speaker aliases. Returns the normalized
    transcript so the caller can render it however suits its format (video
    timeline, markdown transcript, enhanced notes, …).

    Cloud STT is rejected before any audio leaves the machine unless
    ``audio_allow_cloud_stt`` is explicitly enabled (plan §3.1).
    """

    return transcribe_audio_file_detailed(
        filepath,
        config,
        device=device,
        source_label=source_label,
        source_id=source_id,
    ).transcript


def transcribe_audio_file_detailed(
    filepath: str,
    config: dict[str, Any],
    *,
    device: str | None = None,
    source_label: str | None = None,
    source_id: str | None = None,
) -> AudioTranscriptionRun:
    """Transcribe one audio file and return metadata needed by renderers."""

    provider_id = resolve_audio_provider(config)
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
    label = str(source_label or Path(filepath).name)
    sid = str(source_id or slug_source_id(label))
    transcript = normalize_transcript(
        raw_payload,
        source_label=label,
        source_id=sid,
        config=config,
    )
    transcript = apply_speaker_aliases(transcript, config.get("audio_speaker_aliases"))
    return AudioTranscriptionRun(
        transcript=transcript,
        provider_id=provider_id,
        capability=capability,
        vocabulary_terms=vocabulary_terms,
        vocabulary_prompt=vocabulary_prompt,
        raw_provider_metadata=dict(raw.provider_metadata),
    )


def resolve_audio_provider(config: dict[str, Any]) -> str:
    """Resolve provider id and enforce local-first/cloud opt-in policy."""

    provider_id = str(config.get("audio_provider") or "local_faster_whisper").strip().lower()
    validate_audio_benchmark_selection(config)
    validate_audio_fusion_selection(config)
    capability = validate_provider_selection(
        provider_id,
        allow_cloud_stt=_truthy(config.get("audio_allow_cloud_stt")),
    )
    validate_audio_diarization_selection(config, capability)
    return provider_id


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
