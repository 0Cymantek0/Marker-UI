"""Provider capability matrix (plan §5.3).

A single declarative table describing what each STT provider can do. The
frontend reads this (via ``/api/settings/audio/capabilities``) to decide which
controls to render and which to disable. The audio converter reads it to know
whether to honour a given option for a given provider.

Capabilities must reflect the *real* wire-level behaviour documented by each
provider, not aspirations. Unknown providers degrade to the local-default
capability set so a typo in a saved preset never crashes a job.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Literal


@dataclass(frozen=True)
class ProviderCapability:
    """What a speech-to-text provider can actually do.

    ``cloud`` drives cloud opt-in enforcement: any provider whose ``cloud`` flag
    is true may only be used when ``audio_allow_cloud_stt`` is explicitly enabled
    by the user. Local providers are always available.
    """

    provider_id: str
    provider_label: str
    runtime_type: str  # local | cloud | local_optional
    cloud: bool
    requires_api_key: bool
    requires_model_license_acceptance: bool
    privacy_level: str  # local | cloud | hybrid
    implementation_state: Literal["implemented", "beta", "deferred", "unsupported"] = "implemented"

    supports_word_timestamps: bool = True
    supports_segment_timestamps: bool = True
    supports_confidence: bool = True
    supports_diarization: bool = False
    supports_speaker_confidence: bool = False
    supports_custom_vocabulary: bool = True
    supports_prompt_context: bool = True
    supports_translation: bool = False
    supports_batch_compare: bool = False

    max_file_size_hint_mb: int | None = None
    default_model: str | None = None


_LOCAL_FASTER_WHISPER = ProviderCapability(
    provider_id="local_faster_whisper",
    provider_label="Local faster-whisper",
    runtime_type="local",
    cloud=False,
    requires_api_key=False,
    requires_model_license_acceptance=False,
    privacy_level="local",
    supports_diarization=False,
    supports_translation=False,
    default_model="tiny.en",
)

_LOCAL_WHISPERX = ProviderCapability(
    provider_id="local_whisperx",
    provider_label="Local WhisperX / pyannote",
    runtime_type="local_optional",
    cloud=False,
    requires_api_key=False,
    requires_model_license_acceptance=True,
    privacy_level="local",
    implementation_state="deferred",
    supports_diarization=True,
    supports_speaker_confidence=True,
    supports_translation=False,
    default_model="large-v3",
)

_OPENAI = ProviderCapability(
    provider_id="openai",
    provider_label="OpenAI Speech-to-Text",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_confidence=False,
    supports_diarization=False,
    supports_speaker_confidence=False,
    supports_translation=True,
    max_file_size_hint_mb=25,
    default_model="gpt-4o-mini-transcribe",
)

_GROQ = ProviderCapability(
    provider_id="groq",
    provider_label="Groq Whisper",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_confidence=True,
    supports_diarization=False,
    supports_speaker_confidence=False,
    supports_translation=False,
    max_file_size_hint_mb=25,
    default_model="whisper-large-v3",
)

_DEEPGRAM = ProviderCapability(
    provider_id="deepgram",
    provider_label="Deepgram Nova",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_diarization=True,
    supports_speaker_confidence=True,
    supports_prompt_context=False,
    supports_translation=False,
    max_file_size_hint_mb=500,
    default_model="nova-3",
)

_ASSEMBLYAI = ProviderCapability(
    provider_id="assemblyai",
    provider_label="AssemblyAI",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_confidence=False,
    supports_diarization=True,
    supports_speaker_confidence=False,
    supports_prompt_context=False,
    supports_translation=False,
    max_file_size_hint_mb=5000,
    default_model="best",
)

_AZURE = ProviderCapability(
    provider_id="azure",
    provider_label="Azure Speech fast transcription",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_confidence=False,
    supports_diarization=True,
    supports_speaker_confidence=False,
    supports_prompt_context=False,
    supports_translation=False,
    max_file_size_hint_mb=200,
    default_model="fast-transcription",
)

_CUSTOM_OPENAI_COMPATIBLE = ProviderCapability(
    provider_id="custom_openai_compatible",
    provider_label="Custom OpenAI-compatible STT",
    runtime_type="cloud",
    cloud=True,
    requires_api_key=True,
    requires_model_license_acceptance=False,
    privacy_level="cloud",
    implementation_state="deferred",
    supports_confidence=False,
    supports_diarization=False,
    supports_speaker_confidence=False,
    supports_translation=False,
    max_file_size_hint_mb=25,
    default_model="whisper-1",
)


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    cap.provider_id: cap
    for cap in (
        _LOCAL_FASTER_WHISPER,
        _LOCAL_WHISPERX,
        _OPENAI,
        _GROQ,
        _DEEPGRAM,
        _ASSEMBLYAI,
        _AZURE,
        _CUSTOM_OPENAI_COMPATIBLE,
    )
}

# The single capability the rest of the app treats as the default. Aliased so
# callers read intent ("the local default") rather than a magic string.
DEFAULT_PROVIDER_ID = "local_faster_whisper"


def get_capability(provider_id: str | None) -> ProviderCapability:
    """Return capability for a provider, falling back to the local default.

    A mistyped or unknown id must never crash a conversion; it falls back to the
    local default provider so the job still produces output.
    """
    if provider_id and provider_id in PROVIDER_CAPABILITIES:
        return PROVIDER_CAPABILITIES[provider_id]
    return PROVIDER_CAPABILITIES[DEFAULT_PROVIDER_ID]


def list_capabilities() -> list[ProviderCapability]:
    """Return every declared provider capability, ordered by id."""
    return sorted(PROVIDER_CAPABILITIES.values(), key=lambda cap: cap.provider_id)


def cap_to_dict(cap: ProviderCapability) -> dict[str, object]:
    """Serialize one capability record for the settings/capability API."""
    return {field.name: getattr(cap, field.name) for field in fields(cap)}


def capabilities_payload() -> list[dict[str, object]]:
    """Serialize the whole matrix for the settings/capability API."""
    from app.audio.providers.registry import available_provider_ids

    available = set(available_provider_ids())
    payload = []
    for cap in list_capabilities():
        row = cap_to_dict(cap)
        row["available"] = cap.provider_id in available
        if row["available"] and row["implementation_state"] == "deferred":
            row["implementation_state"] = "implemented"
        payload.append(row)
    return payload
