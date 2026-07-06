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

from pathlib import Path
from typing import Any

from app.audio.ingest import probe_audio
from app.audio.pipeline import AudioTranscript, normalize_transcript, slug_source_id
from app.audio.providers import build_provider, get_capability
from app.audio.providers.registry import validate_audio_benchmark_selection
from app.audio.speakers import apply_speaker_aliases
from app.audio.vocabulary import compile_vocabulary_prompt, resolve_vocabulary_terms
from app.conversion.converters.audio import _resolve_provider, _truthy


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

    validate_audio_benchmark_selection(config)
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

    label = str(source_label or Path(filepath).name)
    sid = str(source_id or slug_source_id(label))
    transcript = normalize_transcript(
        raw_payload,
        source_label=label,
        source_id=sid,
        config=config,
    )
    return apply_speaker_aliases(transcript, config.get("audio_speaker_aliases"))
