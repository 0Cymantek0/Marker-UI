"""Provider adapter registry + factory.

Mirrors :func:`app.services.ocr_engine.build_ocr_engine`: a single factory maps
the ``audio_provider`` config string to a concrete adapter. Only
``local_faster_whisper`` ships today; cloud providers land one adapter at a time
behind the same :class:`AudioTranscriptionProvider` interface.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Callable

from app.audio.providers.base import AudioTranscriptionProvider
from app.audio.providers.capabilities import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_CAPABILITIES,
    ProviderCapability,
    get_capability,
)

__all__ = [
    "DEFAULT_PROVIDER_ID",
    "all_advertised_provider_ids",
    "available_provider_ids",
    "build_provider",
    "get_capability",
    "validate_audio_benchmark_selection",
    "validate_provider_selection",
]


# Provider ids that the capability matrix advertises but whose adapters are not
# shipped yet. A cloud STT provider is only wired up after its adapter +
# capability flags + fixtures land together (plan §5.4). Until then we raise a
# clear NotImplementedError instead of silently degrading to faster-whisper, so a
# user who explicitly chose cloud never gets a quiet local fallback.
_DEFERRED_PROVIDERS: dict[str, str] = {
    "openai": "OpenAI Speech-to-Text",
    "groq": "Groq Whisper-compatible STT",
    "deepgram": "Deepgram pre-recorded transcription",
    "assemblyai": "AssemblyAI speaker-labelled transcription",
    "azure": "Azure fast transcription",
    "local_whisperx": "Local WhisperX/pyannote diarization route",
    "custom_openai_compatible": "Custom OpenAI-compatible STT",
}

# Lazy registry: provider id -> adapter factory. Importing every adapter up front
# would pull heavy optional deps (faster_whisper, httpx clients) into every
# process; we only build what a job actually requests.
AdapterFactory = Callable[[], AudioTranscriptionProvider]


@lru_cache(maxsize=1)
def _local_factories() -> dict[str, AdapterFactory]:
    """Built-in adapter factories. Local-first providers only by default."""

    def _faster_whisper() -> AudioTranscriptionProvider:
        from app.audio.providers.faster_whisper import FasterWhisperProvider

        return FasterWhisperProvider()

    return {"local_faster_whisper": _faster_whisper}


def available_provider_ids() -> list[str]:
    """Provider ids with a shipped adapter (capabilities that actually resolve)."""

    return sorted(_local_factories())


def all_advertised_provider_ids() -> list[str]:
    """Every id in the capability matrix — shipped or deferred."""

    return sorted(PROVIDER_CAPABILITIES)


def build_provider(provider_id: str | None) -> AudioTranscriptionProvider:
    """Resolve *provider_id* to a concrete adapter.

    Defaults to ``local_faster_whisper`` (plan §3.1: local-first by default). A
    deferred cloud provider raises :class:`NotImplementedError` so the UI never
    silently falls back to local when the user explicitly asked for cloud.
    """

    key = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    if key in _DEFERRED_PROVIDERS:
        raise NotImplementedError(_deferred_provider_message(key))
    factories = _local_factories()
    factory = factories.get(key)
    if factory is None:
        raise ValueError(_unknown_provider_message(key))
    return factory()


def validate_provider_selection(
    provider_id: str | None,
    *,
    allow_cloud_stt: bool = False,
) -> ProviderCapability:
    """Validate a requested provider before a job is queued.

    Unknown ids and declared-but-deferred providers fail early because selecting
    them would otherwise create a queued job that either silently changes
    provider or only fails inside the worker.
    """

    key = (provider_id or DEFAULT_PROVIDER_ID).strip().lower()
    if key in _DEFERRED_PROVIDERS:
        raise NotImplementedError(_deferred_provider_message(key))
    if key not in PROVIDER_CAPABILITIES:
        raise ValueError(_unknown_provider_message(key))
    capability = get_capability(key)
    if capability.cloud and not allow_cloud_stt:
        raise PermissionError(
            f"Audio provider {key!r} is cloud-based but cloud STT is not enabled. "
            "Enable 'allow cloud STT' to send audio to this provider."
        )
    return capability


def validate_audio_benchmark_selection(config: dict[str, object]) -> None:
    """Reject provider comparison until the comparison runner ships.

    Provider capabilities describe STT adapters, not a full benchmark executor.
    Keeping this explicit prevents CLI/MCP/REST callers from enabling a flag
    that would otherwise pass through conversion without changing output.
    """

    if not _truthy(config.get("audio_benchmark_compare")):
        return
    raise NotImplementedError(
        "Audio provider comparison is not shipped in this build. "
        "It requires a benchmark runner plus at least two shipped STT adapters; "
        "disable audio_benchmark_compare."
    )


def _deferred_provider_message(provider_id: str) -> str:
    return (
        f"Audio provider {provider_id!r} ({_DEFERRED_PROVIDERS[provider_id]}) is not shipped yet. "
        "It is gated behind its adapter + capability flags + provider fixtures "
        "landing together. Configure it as a provider record and enable cloud STT "
        "once its adapter ships."
    )


def _unknown_provider_message(provider_id: str) -> str:
    return (
        f"Unknown audio provider {provider_id!r}. "
        f"Known providers: {', '.join(all_advertised_provider_ids())}."
    )


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
