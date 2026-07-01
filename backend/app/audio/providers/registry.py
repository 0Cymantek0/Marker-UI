"""Provider adapter registry + factory.

Mirrors :func:`app.services.ocr_engine.build_ocr_engine`: a single factory maps
the ``audio_provider`` config string to a concrete adapter. Only
``local_faster_whisper`` ships today; cloud providers land one adapter at a time
behind the same :class:`AudioTranscriptionProvider` interface.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Callable

from app.audio.providers.base import AudioTranscriptionProvider
from app.audio.providers.capabilities import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_CAPABILITIES,
    ProviderCapability,
    get_capability,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROVIDER_ID",
    "all_advertised_provider_ids",
    "available_provider_ids",
    "build_provider",
    "get_capability",
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
        raise NotImplementedError(
            f"Audio provider {key!r} ({_DEFERRED_PROVIDERS[key]}) is not shipped yet. "
            "It is gated behind its adapter + capability flags + provider fixtures "
            "landing together. Configure it as a provider record and enable cloud STT "
            "once its adapter ships."
        )
    factories = _local_factories()
    factory = factories.get(key)
    if factory is None:
        logger.warning(
            "Unknown audio provider %r; falling back to %s", key, DEFAULT_PROVIDER_ID
        )
        factory = factories[DEFAULT_PROVIDER_ID]
    return factory()
